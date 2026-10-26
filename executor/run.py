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

from executor.engine import compute_diff, execute, render_diff
from executor.listmonk import ListmonkSettings, client_from_settings
from executor.plan import PlanError, build_plan
from executor.target import OfflineTarget, TargetClient, TargetError
from ingest.mailchimp import DEFAULT_ACCOUNT_TZ, load_bundle
from mapping.gate import GateError, load_approved

FIXTURE_AUDIENCE = Path("fixtures/mailchimp_export/audience")
FIXTURE_SUPPLEMENT = Path("fixtures/api_supplement")


def pick_client(settings: ListmonkSettings, need_writes: bool) -> TargetClient:
    if settings.configured:
        return client_from_settings(settings)
    if need_writes:
        raise TargetError(
            "--execute needs a configured target (ESP_LISTMONK_URL, "
            "ESP_LISTMONK_USERNAME, ESP_LISTMONK_PASSWORD)"
        )
    print("note: no target configured; diffing against an assumed-empty target\n")
    return OfflineTarget()


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
    args = parser.parse_args()

    try:
        spec = load_approved(args.spec)
        bundle = load_bundle(args.audience_dir, args.supplement_dir, args.account_tz)
        plan = build_plan(bundle, spec)
        client = pick_client(ListmonkSettings(), need_writes=args.execute)

        list_id = client.ensure_list(plan.list_name)
        diff = compute_diff(
            plan, client.current_subscribers(list_id), client.current_campaigns()
        )
        print(render_diff(diff))
        if not args.execute:
            print("\ndry run only; pass --execute to write")
            return 0

        summary = execute(plan, client)
        writes = ", ".join(f"{k}: {v:,}" for k, v in sorted(summary.written.items()))
        print(
            f"\nexecuted into list {summary.list_id}: {writes or 'nothing'} "
            f"({summary.skipped_unchanged:,} already converged)"
        )
        return 0
    except (GateError, PlanError, TargetError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
