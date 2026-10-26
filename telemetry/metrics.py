"""Run metrics to the Datadog v2 series API (RC1-548).

The monitors, SLOs, and service dashboard (tpm-automation-platform/datadog/)
read these series: pipeline runs by stage and outcome, gate approvals,
executor write volume and write errors, reconciliation results.

Decoration like llmobs: without DD_API_KEY in the process environment,
`count()` declines and the run is unaffected. But never silently - the v2
series endpoint accepts `type` as an integer enum (1 = count) and answers a
malformed payload with a 400 that, if swallowed, is indistinguishable from no
traffic (the RC1-440 lesson). A failed POST prints loudly to stderr and
returns False; it never raises, because telemetry must not fail a migration.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from telemetry.llmobs import ML_APP, package_version

METRIC_PREFIX = "esp_migration."
_COUNT = 1  # the v2 series integer enum: 0 unspecified, 1 count, 2 rate, 3 gauge


def _base_tags() -> list[str]:
    return [
        f"service:{ML_APP}",
        f"env:{os.environ.get('DD_ENV', 'prod')}",
        f"version:{os.environ.get('DD_VERSION') or package_version()}",
    ]


def series_payload(name: str, value: float, tags: list[str], at: int | None = None) -> dict:
    """One count series, shaped for POST /api/v2/series. Pure, for tests."""
    return {
        "series": [
            {
                "metric": METRIC_PREFIX + name,
                "type": _COUNT,
                "points": [{"timestamp": at or int(time.time()), "value": value}],
                "tags": _base_tags() + tags,
            }
        ]
    }


def count(name: str, value: float = 1, *, tags: dict[str, str] | None = None) -> bool:
    """Submit one count point, or quietly decline without a key.

    Returns whether the point was accepted. `tags` are key -> value pairs
    joined onto the base service/env/version set.
    """
    api_key = os.environ.get("DD_API_KEY")
    if not api_key:
        return False
    payload = series_payload(name, value, [f"{k}:{v}" for k, v in sorted((tags or {}).items())])
    try:
        response = httpx.post(
            f"https://api.{os.environ.get('DD_SITE', 'datadoghq.com')}/api/v2/series",
            headers={"DD-API-KEY": api_key, "Content-Type": "application/json"},
            content=json.dumps(payload),
            timeout=10,
        )
    except httpx.HTTPError as exc:
        print(f"telemetry: metric {name} not sent: {exc}", file=sys.stderr)
        return False
    if response.status_code >= 300:
        print(
            f"telemetry: metric {name} rejected with HTTP {response.status_code}: "
            f"{response.text[:200]}",
            file=sys.stderr,
        )
        return False
    return True


@contextmanager
def stage(name: str, **tags: str) -> Iterator[None]:
    """Count one `pipeline_run` for the wrapped stage, outcome included.

    An exception counts as `outcome:failure` and propagates unchanged - the
    CLI's own error handling stays the source of truth for what the user
    sees; this only makes the failure visible to the pipeline-failure monitor.
    """
    try:
        yield
    except BaseException:
        count("pipeline_run", tags={"stage": name, "outcome": "failure", **tags})
        raise
    count("pipeline_run", tags={"stage": name, "outcome": "success", **tags})
