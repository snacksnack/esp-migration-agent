"""Cutover plan artifact (RC1-546): migrations are deliveries, not scripts.

Renders the customer-facing cutover plan from the approved spec and the plan:
freeze window, phase-ordered migration steps, parallel-run validation, a
cutover checklist, an explicit rollback section (what is undone and how, and
what cannot be), and the Phase 1 send-ramp recommendation as its sending
section. Every count traces to the deterministic plan.

Usage: uv run python -m executor.cutover --spec specs/approved/<spec>.yaml
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from discovery.profile import build_profile
from discovery.report import ramp_recommendation
from executor.plan import MigrationPlan, build_plan
from ingest.mailchimp import DEFAULT_ACCOUNT_TZ, load_bundle
from ingest.models import MailchimpBundle
from mapping.gate import load_approved
from specs.model import MappingSpec

FIXTURE_AUDIENCE = Path("fixtures/mailchimp_export/audience")
FIXTURE_SUPPLEMENT = Path("fixtures/api_supplement")


def render_cutover_plan(
    plan: MigrationPlan,
    spec: MappingSpec,
    ramp: str,
    freeze_hours: int = 24,
    parallel_run_days: int = 7,
) -> str:
    approved_line = (
        f"Approved by {spec.approval.approved_by} on "
        f"{spec.approval.approved_at.date().isoformat()} "
        f"(content sha256 `{spec.approval.content_sha256[:12]}...`)."
        if spec.approval
        else "Spec not yet approved - this plan is a preview."
    )
    open_questions = spec.open_questions()
    return f"""# Cutover plan: {plan.source} -> {plan.target}

{approved_line} Executor runs exactly this content; any spec edit requires
re-approval before the next run.

## Scope

- Suppressions (phase 1, gating): {len(plan.suppressions):,}
- Subscribers (phase 2): {len(plan.subscribers):,}
- Draft campaigns (phase 3): {len(plan.campaigns)}
- Segment verification queries: {len(plan.segments)}
- Open mapping questions at approval: {len(open_questions)} (must be 0 - enforced)

## Freeze window ({freeze_hours}h)

1. Announce the freeze: no audience edits, imports, signup-form changes, or
   sends in the source account during the window.
2. Pull a fresh source export at freeze start; re-run discovery against it
   and diff the counts against this plan's scope (deltas since the planning
   export migrate in this run; deltas after it do not exist by definition).
3. Keep source signup forms live - new signups during the freeze land in the
   source and migrate in the final delta pass (step 4 of the checklist).

## Migration order (the executor enforces phases 1-3)

1. **Suppressions first.** Every suppressed address lands non-subscribable
   before any subscriber is written. This ordering is structural in the
   executor and the spec schema; it is not an operator step that can be
   missed.
2. Subscribers with attributes, consent states per the approved spec.
3. Draft campaigns with translated merge tags (nothing is sent).
4. Final delta pass: re-run the executor against a fresh export; upserts
   converge, so only freeze-window deltas write.

## Parallel-run validation ({parallel_run_days} days)

- Target sending stays OFF; source continues to serve any transactional mail.
- Run the reconciliation report daily
  (`python -m executor.reconcile --spec <approved spec>`): consent counts,
  100% suppression integrity, segment parity, sampled checksums, campaign
  render comparison. Any FAIL stops the cutover clock.
- Unsubscribes collected in the source during the parallel run are re-synced
  by re-running the executor (suppression phase picks them up first).

## Cutover checklist

- [ ] Freeze announced; fresh export pulled; discovery diff reviewed
- [ ] Executor dry-run diff reviewed (no unexpected creates/updates)
- [ ] Executor run complete; write summary archived
- [ ] Reconciliation report PASS archived
- [ ] Second executor run converges to zero writes (idempotency proof)
- [ ] Final delta pass complete after freeze end
- [ ] Signup forms and integrations repointed to the target
- [ ] First target send per the ramp below; bounce/complaint monitoring live
- [ ] Source account set read-only (retain for the retention period, then close)

## Rollback

**What rolls back, and how.** The source is never written: the executor has
no write path to {plan.source}, so the source account remains a complete,
untouched system of record throughout. Rollback is therefore: repoint signup
forms and sending back to the source, delete the target list and its
subscribers (one API call each), and schedule a re-run. Suppression events
collected ONLY in the target during parallel run must be exported back to the
source before the wipe - this is the one data path that must be preserved.

**What cannot be rolled back.** Mail already sent from the target (including
any opt-in confirmations), and the reputation signals those sends generated.
This is why target sending stays off until the parallel-run reconciliation
passes.

## Sending ramp

{ramp}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True, help="APPROVED spec yaml")
    parser.add_argument("--audience-dir", type=Path, default=FIXTURE_AUDIENCE)
    parser.add_argument("--supplement-dir", type=Path, default=FIXTURE_SUPPLEMENT)
    parser.add_argument("--account-tz", default=DEFAULT_ACCOUNT_TZ)
    parser.add_argument("--freeze-hours", type=int, default=24)
    parser.add_argument("--parallel-run-days", type=int, default=7)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    spec = load_approved(args.spec)
    bundle: MailchimpBundle = load_bundle(
        args.audience_dir, args.supplement_dir, args.account_tz
    )
    plan = build_plan(bundle, spec)
    ramp = ramp_recommendation(build_profile(bundle, as_of=datetime.now(UTC)))
    markdown = render_cutover_plan(
        plan, spec, ramp,
        freeze_hours=args.freeze_hours,
        parallel_run_days=args.parallel_run_days,
    )
    if args.out:
        args.out.write_text(markdown)
        print(f"wrote {args.out}")
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
