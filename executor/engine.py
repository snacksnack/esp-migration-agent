"""Diff and execute (RC1-546): the only module that writes to a target.

Order is law here: suppressions are written before any subscriber, by
iterating the plan's phases in their structural order. The write loop
re-checks the suppression invariant per record even though build_plan already
proved it - the invariant is cheap and the cost of being wrong is the whole
project's thesis.

Idempotency: every write is an upsert keyed on email (campaigns: name), and
a record that already matches the desired state is skipped. Re-running
against a partially-migrated target converges to zero writes.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:  # journal imports engine's models; avoid the runtime cycle
    from executor.journal import ExecutionJournal

from executor.plan import (
    DesiredSubscriber,
    MigrationPlan,
    PlannedCampaign,
    SuppressionViolationError,
    existing_record_protected,
    target_state_subscribable,
)
from executor.target import TargetClient, TargetSubscriber


def subscriber_converged(existing: TargetSubscriber, desired: DesiredSubscriber) -> bool:
    """The executor owns name, status, subscription, and the attribs keys it
    writes; target-side extras are left alone and do not block convergence."""
    return (
        existing.name == desired.name
        and existing.status == desired.status
        and existing.subscription == desired.subscription
        and all(existing.attribs.get(k) == v for k, v in desired.attribs.items())
    )


class DiffEntry(BaseModel):
    phase: str
    # create | update | unchanged | conflict (kept, reported) | attach (a
    # conflict off this list: membership added, consent untouched)
    action: str
    key: str  # email or campaign name


class PlanDiff(BaseModel):
    entries: list[DiffEntry]

    def counts(self) -> dict[str, dict[str, int]]:
        by_phase: dict[str, Counter] = {}
        for e in self.entries:
            by_phase.setdefault(e.phase, Counter())[e.action] += 1
        return {phase: dict(c) for phase, c in by_phase.items()}

    @property
    def writes(self) -> list[DiffEntry]:
        return [e for e in self.entries if e.action in ("create", "update", "attach")]


def _subscriber_action(
    desired: DesiredSubscriber,
    current: dict[str, TargetSubscriber],
    target: str,
    elsewhere: dict[str, TargetSubscriber] | None = None,
) -> str:
    key = desired.email.lower()
    existing = current.get(key)
    if existing is None:
        other = (elsewhere or {}).get(key)
        if other is None:
            return "create"
        # On the target but not on this list (another audience, or the
        # customer's own lists). Contacts are global: creating would collide
        # or upsert over it. Protected -> "attach": a conflict that also adds
        # the membership, consent untouched; otherwise update it onto this
        # list, other memberships preserved (RC1-573, ADR-0018).
        if desired.subscribable and existing_record_protected(
            target, other.status, other.subscription
        ):
            return "attach"
        return "update"
    if subscriber_converged(existing, desired):
        return "unchanged"
    if desired.subscribable and not target_state_subscribable(
        target, existing.status, existing.subscription
    ):
        # The destination already protects this address more strictly than
        # the plan wants it: never downgrade protection (ADR-0013). The
        # record is kept as the target holds it and reported as a conflict.
        return "conflict"
    return "update"


def _campaign_action(
    campaign: PlannedCampaign, current: dict[str, PlannedCampaign]
) -> str:
    existing = current.get(campaign.name)
    if existing is None:
        return "create"
    same = existing.subject == campaign.subject and existing.body == campaign.body
    return "unchanged" if same else "update"


def compute_diff(
    plan: MigrationPlan,
    current_subscribers: dict[str, TargetSubscriber],
    current_campaigns: dict[str, PlannedCampaign],
    elsewhere: dict[str, TargetSubscriber] | None = None,
) -> PlanDiff:
    entries = [
        DiffEntry(
            phase="1-suppressions" if d.suppressed else "2-subscribers",
            action=_subscriber_action(d, current_subscribers, plan.target, elsewhere),
            key=d.email,
        )
        for d in plan.all_subscribers
    ]
    entries += [
        DiffEntry(
            phase="3-campaigns",
            action=_campaign_action(c, current_campaigns),
            key=c.name,
        )
        for c in plan.campaigns
    ]
    return PlanDiff(entries=entries)


def render_diff(diff: PlanDiff, max_examples: int = 5) -> str:
    lines: list[str] = ["dry-run diff (phase order is execution order):"]
    for phase, counts in sorted(diff.counts().items()):
        summary = ", ".join(f"{action}: {n:,}" for action, n in sorted(counts.items()))
        lines.append(f"  {phase}: {summary}")
        examples = [e for e in diff.entries if e.phase == phase and e.action != "unchanged"]
        for e in examples[:max_examples]:
            lines.append(f"    {e.action}  {e.key}")
        if len(examples) > max_examples:
            lines.append(f"    ... and {len(examples) - max_examples:,} more")
    conflicts = [e for e in diff.entries if e.action in ("conflict", "attach")]
    if conflicts:
        attached = sum(1 for e in conflicts if e.action == "attach")
        lines.append(
            f"  {len(conflicts):,} conflict(s): the destination already holds "
            "these addresses in a stricter consent state; the executor keeps "
            "that state and never downgrades protection"
            + (
                f" ({attached:,} held elsewhere on the target are attached to "
                "this list with that state)"
                if attached
                else ""
            )
        )
    if not diff.writes:
        lines.append("  target already converged: nothing to write")
    return "\n".join(lines)


class ExecutionSummary(BaseModel):
    list_id: int
    written: dict[str, int]  # action -> count, writes only
    skipped_unchanged: int
    conflicts: list[str] = []  # kept at the destination's stricter consent


def execute(
    plan: MigrationPlan,
    client: TargetClient,
    journal: ExecutionJournal | None = None,
) -> ExecutionSummary:
    """Write the plan to the target, suppressions first, upserts throughout.
    Each completed write lands in the journal before the next one starts, so
    an interrupted run's record ends exactly where the run did (RC1-571)."""
    list_id = client.ensure_list(plan.list_name)
    current = client.current_subscribers(list_id)
    elsewhere = client.lookup_subscribers(
        [d.email for d in plan.all_subscribers if d.email.lower() not in current]
    )
    if journal is not None:
        # The destination as found, before the first write (RC1-574): what
        # reconciliation later accepts as pre-existing, and nothing else.
        journal.baseline(list_id, current, elsewhere)
    written: Counter = Counter()
    skipped = 0
    conflicts: list[str] = []
    # Adapters count read-back recoveries (RC1-570); a delta across one write
    # means that write's response was lost and the recovery stood in for it.
    recovered_seen = getattr(client, "recovered_calls", 0)

    def journal_write(phase: str, action: str, key: str) -> None:
        nonlocal recovered_seen
        recovered_now = getattr(client, "recovered_calls", 0)
        if journal is not None:
            journal.write(phase, action, key, recovered=recovered_now > recovered_seen)
        recovered_seen = recovered_now

    # Phases 1 then 2, in the plan's structural order.
    for desired in plan.all_subscribers:
        if desired.suppressed and desired.subscribable:
            raise SuppressionViolationError(
                f"write-time invariant check: {desired.email} is suppressed but "
                "planned subscribable; aborting before the write"
            )
        phase = "1-suppressions" if desired.suppressed else "2-subscribers"
        key = desired.email.lower()
        action = _subscriber_action(desired, current, plan.target, elsewhere)
        if action == "create":
            client.create_subscriber(desired, list_id)
        elif action == "update":
            client.update_subscriber(current.get(key) or elsewhere[key], desired, list_id)
        elif action == "attach":
            client.attach_subscriber(elsewhere[key], list_id)
            conflicts.append(desired.email)
            if journal is not None:
                journal.conflict(phase, desired.email)
        elif action == "conflict":
            conflicts.append(desired.email)
            if journal is not None:
                journal.conflict(phase, desired.email)
            continue
        else:
            skipped += 1
            continue
        written[action] += 1
        journal_write(phase, action, desired.email)

    current_campaigns = client.current_campaigns()
    for campaign in plan.campaigns:
        action = _campaign_action(campaign, current_campaigns)
        if action == "create":
            client.create_campaign(campaign, list_id)
        elif action == "update":
            client.update_campaign(campaign.name, campaign, list_id)
        else:
            skipped += 1
            continue
        written[action] += 1
        journal_write("3-campaigns", action, campaign.name)

    return ExecutionSummary(
        list_id=list_id,
        written=dict(written),
        skipped_unchanged=skipped,
        conflicts=conflicts,
    )
