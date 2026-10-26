"""Datadog LLM Observability for the pipeline's model calls (RC1-548).

Decoration, never load-bearing: `enable_llmobs()` is a no-op returning False
unless both halves are present - ddtrace installed (the `obs` dependency
group) and DD_API_KEY in the process environment - so every CLI calls it
unconditionally and stays runnable on a machine with neither. Agentless by
design: these are laptop CLI runs with no local Datadog agent daemon.

Fleet conventions this module implements:
- One ml_app per agent (`esp-migration-agent`), so Datadog's LLM Obs app list
  is the fleet inventory (agent-evals RC1-322).
- The intake indexes a root (workflow) span's tags onto the trace metrics
  ONLY when the payload carries a `version:` tag (RC1-407). ddtrace sends it
  from DD_VERSION, so `enable_llmobs()` defaults DD_VERSION to this repo's
  package version - without it every root span's tags are silently dropped.
- Non-anthropic LLM integrations are env-defaulted off before enable()
  patches, because `LLMObs.enable()` patches with raise_errors=True and a
  module-name collision would crash the run tracing was meant to decorate
  (agent-evals RC1-331). Both LLM calls here use `Messages.create`, which the
  anthropic integration traces natively.
"""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path

try:  # documented optional-dep exception: the `obs` group may be absent
    from ddtrace import config as _dd_config
    from ddtrace.llmobs import LLMObs
except ImportError:  # pragma: no cover - exercised only without the group
    _dd_config = None
    LLMObs = None

ML_APP = "esp-migration-agent"

_enabled = False


def package_version() -> str:
    """The repo's pyproject version: the `version:` tag on every span and
    metric, so a regression is attributable to a release."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def apply_version() -> str:
    """Set DD_VERSION and ddtrace's own config to the package version.

    ddtrace reads DD_VERSION at import time, which is before any of this code
    runs - set only the env var and the version tag goes out empty (measured:
    the first traced run indexed with version:"", exactly the RC1-407 drop
    this guards). Setting `config.version` works post-import. Also called by
    the eval CLI, whose tracing is agent-evals' enable, not ours.
    """
    version = os.environ.setdefault("DD_VERSION", package_version())
    if _dd_config is not None and not _dd_config.version:
        _dd_config.version = version
    return version


def active() -> bool:
    """Whether `enable_llmobs()` has run and succeeded in this process."""
    return _enabled


def enable_llmobs(*, service: str | None = None) -> bool:
    """Turn on LLM Observability for this process, or quietly decline.

    `service` defaults to the ml_app; eval runs pass their own so the two
    populations stay separable by the `service` tag in Datadog.
    """
    global _enabled
    if _enabled:
        return True
    if LLMObs is None or not os.environ.get("DD_API_KEY"):
        return False
    apply_version()
    _restrict_patching_to_anthropic()
    try:
        LLMObs.enable(
            ml_app=ML_APP,
            agentless_enabled=True,
            site=os.environ.get("DD_SITE", "datadoghq.com"),
            service=service or ML_APP,
        )
    except Exception as exc:  # a run must never die for its decoration
        print(f"telemetry: llmobs disabled, enable() failed: {exc}", file=sys.stderr)
        return False
    _enabled = True
    return True


def _llm_integration_modules() -> tuple[str, ...]:
    """The module names `LLMObs.enable()` would patch; empty when unknown.

    Read from ddtrace's own constants so the set tracks the installed version.
    Private imports, guarded: on a moved layout we fall back to patching
    everything, and the try/except in `enable_llmobs()` keeps the run alive.
    """
    try:
        from ddtrace.llmobs._constants import SUPPORTED_LLMOBS_INTEGRATIONS
        from ddtrace.llmobs._llmobs import _INTEGRATIONS_W_PROPAGATION_SUPPORT
    except ImportError:  # pragma: no cover - exercised only on a moved layout
        return ()
    return tuple(
        set(SUPPORTED_LLMOBS_INTEGRATIONS.values())
        | set(_INTEGRATIONS_W_PROPAGATION_SUPPORT.values())
    )


def _restrict_patching_to_anthropic() -> None:
    """setdefault, not setenv: an explicit `DD_TRACE_<X>_ENABLED` still wins."""
    for module in _llm_integration_modules():
        if module == "anthropic":
            continue
        os.environ.setdefault(f"DD_TRACE_{module.upper().replace('-', '_')}_ENABLED", "false")


@contextmanager
def workflow(name: str) -> Iterator[None]:
    """One pipeline stage as a root workflow span; a no-op when tracing is off.

    The root span is what the stage costs end to end in the trace list; the
    anthropic llm span nests under it automatically. Named for the stage
    (`discovery.report`, `mapping.propose`), not the model call.
    """
    if not _enabled:
        with nullcontext():
            yield
        return
    with LLMObs.workflow(name=name):
        yield
