"""`python -m evals` - the billed suite (RC1-548).

pytest is the free layer and needs no key; this binds the frozen case
payloads into the shipped proposer call and scores the real model's real
output. Needs ESP_ANTHROPIC_API_KEY (the repo's own spelling, resolved by
ProposerSettings - ADR-0035: credential resolution stays in the consumer).
Run with the `evals` dependency group: `uv run --group evals python -m evals`.

Records land in the shared store (`EVAL_DATABASE_URL`, else local JSONL);
publish the trend page after a run: `agent-evals/scripts/publish_trend.sh`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from agent_evals import llmobs
from agent_evals.budget import breaches_for
from agent_evals.runner import UnknownCase, exit_code, print_result, record_run, select_cases

from evals import subject
from mapping.proposer import ProposalError, ProposerSettings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals", description=__doc__)
    parser.add_argument("--case", help="run a single case by id")
    args = parser.parse_args(argv)

    if not ProposerSettings().anthropic_api_key:
        print("cannot run: ESP_ANTHROPIC_API_KEY is not set (see .env.example)", file=sys.stderr)
        return 2
    try:
        cases = select_cases(subject.CASES, args.case)
    except UnknownCase as exc:
        print(exc, file=sys.stderr)
        return 2

    version = subject.version()
    print(f"{len(cases)} case(s) against {version.model} — this spends money.\n")
    # Billed spend is traced spend: same ml_app as the pipeline so the fleet
    # pane shows one row per agent, `service:evals` so these runs stay out of
    # the production spend guardrails and the RC1-548 cost monitor. A no-op
    # without DD_API_KEY.
    llmobs.enable("esp-migration-agent", service="evals")
    started = datetime.now(UTC)
    results = []
    for case in cases:
        with llmobs.case(case.id, input_data=case.input) as traced:
            try:
                result = subject.run(case)
            except ProposalError as exc:  # pre-call config problems
                print(f"cannot run {case.id}: {exc}", file=sys.stderr)
                return 2
            traced.record(result)
        results.append(result)
        print_result(result)

    record = record_run(version, started, results)
    for breach in breaches_for(subject.CEILING, record.total_cost_usd, record.total_latency_ms):
        print(f"  ~ budget [advisory]: {breach}")
    print(f"run cost ${record.total_cost_usd} in {record.total_latency_ms / 1000:.0f}s")
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
