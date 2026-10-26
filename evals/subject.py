"""The mapping-quality subject (RC1-548): flag-don't-guess, run for real.

Each case binds a frozen payload (`evals/cases/*.json`) into the shipped
proposer call - `mapping.proposer.request_mapping`, same prompt, model,
limits and error mapping as production - and scores the draft spec that the
shipped validation (`assemble_spec`) builds from the response.

Two cases, two failure modes:

- **fixture-ambiguities** is the real fixture payload. The three seeded
  ambiguities (ZIP formats, LASTPURCH formats, the overlapping VIP segment)
  must come back flagged with open questions: flag *recall*.
- **clean-variant** is the same account with the ambiguities removed. ZIP and
  LASTPURCH now have one defensible answer and must come back proposed: flag
  *precision*. A proposer that flags everything aces the first case and is
  useless; only the pair measures the contract.

Judgment calls (MEMBER_RATING, the consent-scope questions, segments that
inherit flagged fields) are legitimately flaggable either way, so they are
reported advisory and never gate - a flaky gate gets disabled within a week,
which is worse than no gate.

Published numbers (the ticket's acceptance criteria) ride in each result's
`observations`: mapping_accuracy over must_propose, flag_recall over
must_flag, flag_precision as the share of flags that were defensible.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

from agent_evals import pricing
from agent_evals.budget import Ceiling
from agent_evals.case import Case
from agent_evals.record import CaseResult, CharacteristicResult, SubjectVersion, Usage

from evals.expectations import BY_CASE, CaseExpectations, Key
from mapping.proposer import (
    PROMPT_VERSION,
    ProposalError,
    ProposerSettings,
    _parse_json,
    assemble_spec,
    request_mapping,
    response_text,
)
from specs.model import MappingSpec, MappingStatus
from telemetry.llmobs import package_version

NAME = "esp-mapping-quality"

CASES_DIR = Path(__file__).resolve().parent / "cases"

_GATED = (
    "survives-the-shipped-validation",
    "seeded-ambiguities-flagged",
    "unambiguous-not-flagged",
    "unambiguous-targets-correct",
    "flagged-mappings-omit-resolution",
    "exhaustive-coverage",
)

CASES: tuple[Case, ...] = (
    Case(
        id="fixture-ambiguities",
        input={"payload": "fixture_listmonk.json"},
        expect=_GATED,
        tags=("mapping-quality", "recall"),
    ),
    Case(
        id="clean-variant",
        input={"payload": "clean_listmonk.json"},
        # No seeded ambiguities to recall here; the characteristic set drops it.
        expect=tuple(c for c in _GATED if c != "seeded-ambiguities-flagged"),
        tags=("mapping-quality", "precision"),
    ),
)

#: Measured 2026-10-26 on claude-opus-5-5: the full two-case suite cost
#: $0.235 and ran 95s (fixture 51s, clean 43s). Ceiling at roughly 2x the
#: measured run, so a payload or prompt that doubles the spend is a finding
#: before it is a bill.
CEILING = Ceiling(
    subject=NAME,
    max_cost_usd=Decimal("0.50"),
    max_latency_ms=300_000,
    note="~2x the measured 2026-10-26 baseline: $0.235 / 95s for both cases on claude-opus-5-5",
)


def version() -> SubjectVersion:
    return SubjectVersion(
        subject=NAME,
        code_version=package_version(),
        model=ProposerSettings().anthropic_model,
        prompt_version=PROMPT_VERSION,
    )


def load_payload(case: Case) -> dict:
    return json.loads((CASES_DIR / case.input["payload"]).read_text())


def run(case: Case) -> CaseResult:
    """One frozen payload through the shipped call, scored. Billed."""
    payload = load_payload(case)
    expectations = BY_CASE[case.id]
    started = time.monotonic()
    try:
        response = request_mapping(payload)
    except ProposalError as exc:
        # The call itself failed: nothing to score, which is a different
        # problem with a different fix than a bad answer.
        return CaseResult(
            case_id=case.id,
            usage=Usage(latency_ms=(time.monotonic() - started) * 1000),
            error=str(exc),
        )
    latency_ms = (time.monotonic() - started) * 1000
    model = ProposerSettings().anthropic_model
    usage = Usage(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cache_creation_input_tokens=response.usage.cache_creation_input_tokens or 0,
        cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
        cost_usd=pricing.cost_usd(
            model,
            response.usage.input_tokens,
            response.usage.output_tokens,
            cache_creation_input_tokens=response.usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
        ),
        latency_ms=latency_ms,
    )
    try:
        spec = assemble_spec(
            _parse_json(response_text(response)),
            source=payload["source"],
            target=payload["target"],
        )
    except ProposalError as exc:
        # The model answered but the shipped validation refused it - in
        # production nothing downstream would see this draft. Scored, not an
        # error: the model produced output and the output is the defect.
        return CaseResult(
            case_id=case.id,
            usage=usage,
            characteristics=[
                CharacteristicResult(
                    name="survives-the-shipped-validation",
                    passed=False,
                    detail=f"assemble_spec refused the output: {exc}",
                )
            ],
        )
    characteristics = [
        CharacteristicResult(
            name="survives-the-shipped-validation",
            passed=True,
            detail="parsed and validated by the production assemble_spec path",
        )
    ]
    scored, observations = score(spec, payload, expectations)
    characteristics.extend(scored)
    return CaseResult(
        case_id=case.id, usage=usage, characteristics=characteristics, observations=observations
    )


def _index(spec: MappingSpec) -> dict[Key, object]:
    index: dict[Key, object] = {}
    for m in spec.field_mappings:
        index[("field", m.source_field)] = m
    for m in spec.segment_mappings:
        index[("segment", m.source_segment)] = m
    for m in spec.consent_mappings:
        index[("consent", m.source_status)] = m
    return index


def _target_of(mapping) -> str | None:
    return getattr(mapping, mapping.target_attr)


def score(
    spec: MappingSpec, payload: dict, expectations: CaseExpectations
) -> tuple[list[CharacteristicResult], dict]:
    """Deterministic scoring of one draft spec against the case's ground
    truth. No judge anywhere: every check is set membership or a predicate."""
    index = _index(spec)
    results: list[CharacteristicResult] = []

    def flagged(key: Key) -> bool:
        m = index.get(key)
        return m is not None and m.status is MappingStatus.FLAGGED and bool(m.open_question)

    # Flag recall on the seeded ambiguities.
    if expectations.must_flag:
        missed = sorted(k for k in expectations.must_flag if not flagged(k))
        recall = 1 - len(missed) / len(expectations.must_flag)
        results.append(
            CharacteristicResult(
                name="seeded-ambiguities-flagged",
                passed=not missed,
                detail=(
                    f"all {len(expectations.must_flag)} seeded ambiguities flagged "
                    "with open questions"
                    if not missed
                    else "guessed or dropped instead of flagging: "
                    + ", ".join(f"{kind} {name!r}" for kind, name in missed)
                ),
            )
        )
    else:
        recall = None

    # Flag precision: an unambiguous mapping that comes back flagged.
    wrongly_flagged = sorted(
        k
        for k in expectations.must_propose
        if (m := index.get(k)) is not None and m.status is MappingStatus.FLAGGED
    )
    results.append(
        CharacteristicResult(
            name="unambiguous-not-flagged",
            passed=not wrongly_flagged,
            detail=(
                f"none of the {len(expectations.must_propose)} unambiguous mappings flagged"
                if not wrongly_flagged
                else "flagged despite one defensible answer: "
                + ", ".join(f"{kind} {name!r}" for kind, name in wrongly_flagged)
            ),
        )
    )

    # Mapping accuracy on the unambiguous set.
    wrong: list[str] = []
    correct = 0
    for key in sorted(expectations.must_propose):
        m = index.get(key)
        target = _target_of(m) if m is not None else None
        if m is None:
            wrong.append(f"{key[0]} {key[1]!r} missing")
        elif m.status is not MappingStatus.PROPOSED:
            wrong.append(f"{key[0]} {key[1]!r} not proposed")
        elif not target or not expectations.targets[key](target):
            wrong.append(f"{key[0]} {key[1]!r} -> {target!r}")
        else:
            correct += 1
    accuracy = correct / len(expectations.must_propose)
    results.append(
        CharacteristicResult(
            name="unambiguous-targets-correct",
            passed=not wrong,
            detail=(
                f"{correct}/{len(expectations.must_propose)} unambiguous mappings land on "
                "the expected concept"
                + ("" if not wrong else "; wrong: " + "; ".join(wrong))
            ),
        )
    )

    # A flag that smuggles a resolution in is a guess wearing a flag.
    resolved_flags = sorted(
        f"{kind} {name!r}"
        for (kind, name), m in index.items()
        if m.status is MappingStatus.FLAGGED and _target_of(m) is not None
    )
    results.append(
        CharacteristicResult(
            name="flagged-mappings-omit-resolution",
            passed=not resolved_flags,
            detail=(
                "every flagged mapping leaves the target to the reviewer"
                if not resolved_flags
                else "flagged but resolved anyway: " + ", ".join(resolved_flags)
            ),
        )
    )

    # Nothing silently dropped, nothing invented, nothing doubled.
    expected_keys = (
        {("field", f["source_field"]) for f in payload["fields"]}
        | {("segment", s["name"]) for s in payload["segments"]}
        | {("consent", c) for c in payload["consent_buckets"]["observed"]}
    )
    missing = sorted(expected_keys - set(index))
    extra = sorted(set(index) - expected_keys)
    doubled = len(spec.reviewed_mappings()) != len(index)
    coverage_problems = (
        [f"missing {k[0]} {k[1]!r}" for k in missing]
        + [f"invented {k[0]} {k[1]!r}" for k in extra]
        + (["duplicate mappings for one source"] if doubled else [])
    )
    results.append(
        CharacteristicResult(
            name="exhaustive-coverage",
            passed=not coverage_problems,
            detail=(
                f"all {len(expected_keys)} source fields, segments and consent buckets "
                "appear exactly once"
                if not coverage_problems
                else "; ".join(coverage_problems)
            ),
        )
    )

    # The judgment calls, reported but never gating.
    judgment = sorted(set(index) - expectations.must_flag - expectations.must_propose)
    all_flagged = {k for k, m in index.items() if m.status is MappingStatus.FLAGGED}
    if judgment:
        results.append(
            CharacteristicResult(
                name="judgment-calls",
                passed=True,
                advisory=True,
                detail=", ".join(
                    f"{kind} {name!r}: {index[(kind, name)].status.value}"
                    for kind, name in judgment
                ),
            )
        )

    defensible = expectations.must_flag | set(judgment)
    observations = {
        "mapping_accuracy": accuracy,
        "flag_recall": recall,
        "flag_precision": (
            len(all_flagged & defensible) / len(all_flagged) if all_flagged else None
        ),
        "flagged": len(all_flagged),
        "proposed": len(index) - len(all_flagged),
    }
    return results, observations
