"""Executor CLI (RC1-546): dry-run by default, writes only with --execute.

    uv run python -m executor.run --spec specs/approved/<spec>.yaml
    uv run python -m executor.run --spec ... --execute

The spec is read through mapping.gate.load_approved - a draft or tampered
spec never reaches the plan builder. Dry-run renders the full phase-ordered
diff (against the live target when ESP_LISTMONK_* is configured, against an
assumed-empty target otherwise). --execute migrates suppressions first,
upserts throughout, and prints the write summary; a second run converges to
zero writes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from executor.clients import TARGETS, client_for_target
from executor.engine import compute_diff, execute, render_diff
from executor.plan import PlanError, build_plan
from executor.target import TargetError
from ingest.mailchimp import DEFAULT_ACCOUNT_TZ, load_bundle
from mapping.gate import GateError, load_approved, verify_source
from telemetry import count, stage

FIXTURE_AUDIENCE = Path("fixtures/mailchimp_export/audience")
FIXTURE_SUPPLEMENT = Path("fixtures/api_supplement")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True, help="APPROVED spec yaml")
    parser.add_argument("--audience-dir", type=Path, default=FIXTURE_AUDIENCE)
    parser.add_argument("--supplement-dir", type=Path, default=FIXTURE_SUPPLEMENT)
    parser.add_argument("--account-tz", default=DEFAULT_ACCOUNT_TZ)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="write to the target (default is dry-run: show the diff and stop)",
    )
    parser.add_argument(
        "--target",
        choices=TARGETS,
        default="listmonk",
        help="which adapter writes; the plan and engine are target-agnostic",
    )
    args = parser.parse_args()

    mode = "execute" if args.execute else "dry_run"
    try:
        with stage("execute", mode=mode, target=args.target):
            spec = load_approved(args.spec)
            # The approval binds the inputs (RC1-566): the adapter must be the
            # one the spec was reviewed for, and the bundle the one reviewed.
            if args.target != spec.target:
                raise PlanError(
                    f"--target {args.target} does not match the approved spec's "
                    f"target {spec.target!r}; the plan would write one platform's "
                    "consent dialect into another"
                )
            verify_source(spec, args.audience_dir, args.supplement_dir)
            bundle = load_bundle(args.audience_dir, args.supplement_dir, args.account_tz)
            plan = build_plan(bundle, spec)
            client = client_for_target(args.target, need_writes=args.execute)

            # A dry run must not write ANYTHING, the list included: look the
            # list up read-only and diff against empty when it is absent
            # (RC1-564). Only --execute may create it.
            if args.execute:
                list_id = client.ensure_list(plan.list_name)
            else:
                list_id = client.find_list(plan.list_name)
            current = (
                client.current_subscribers(list_id) if list_id is not None else {}
            )
            diff = compute_diff(plan, current, client.current_campaigns())
            print(render_diff(diff))
            if not args.execute:
                if list_id is None:
                    print(f"\nlist {plan.list_name!r} does not exist; --execute would create it")
                print("\ndry run only; pass --execute to write")
                return 0

            try:
                summary = execute(plan, client)
            except TargetError:
                count("executor_write_errors", tags={"target": args.target})
                raise
            count(
                "executor_writes",
                value=sum(summary.written.values()),
                tags={"target": args.target},
            )
        writes = ", ".join(f"{k}: {v:,}" for k, v in sorted(summary.written.items()))
        print(
            f"\nexecuted into list {summary.list_id}: {writes or 'nothing'} "
            f"({summary.skipped_unchanged:,} already converged)"
        )
        if summary.conflicts:
            shown = ", ".join(summary.conflicts[:5])
            more = (
                f" and {len(summary.conflicts) - 5:,} more"
                if len(summary.conflicts) > 5
                else ""
            )
            print(
                f"kept the destination's stricter consent for "
                f"{len(summary.conflicts):,} address(es): {shown}{more} "
                "(ADR-0013: protection is never downgraded)"
            )
        return 0
    except (GateError, PlanError, TargetError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
