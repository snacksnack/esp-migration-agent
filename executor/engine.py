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

from pydantic import BaseModel

from executor.plan import (
    DesiredSubscriber,
    MigrationPlan,
    PlannedCampaign,
    SuppressionViolationError,
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
    action: str  # create | update | unchanged
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
        return [e for e in self.entries if e.action != "unchanged"]


def _subscriber_action(
    desired: DesiredSubscriber, current: dict[str, TargetSubscriber]
) -> str:
    existing = current.get(desired.email.lower())
    if existing is None:
        return "create"
    return "unchanged" if subscriber_converged(existing, desired) else "update"


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
) -> PlanDiff:
    entries = [
        DiffEntry(
            phase="1-suppressions" if d.suppressed else "2-subscribers",
            action=_subscriber_action(d, current_subscribers),
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
    if not diff.writes:
        lines.append("  target already converged: nothing to write")
    return "\n".join(lines)


class ExecutionSummary(BaseModel):
    list_id: int
    written: dict[str, int]  # action -> count, writes only
    skipped_unchanged: int


def execute(plan: MigrationPlan, client: TargetClient) -> ExecutionSummary:
    """Write the plan to the target, suppressions first, upserts throughout."""
    list_id = client.ensure_list(plan.list_name)
    current = client.current_subscribers(list_id)
    written: Counter = Counter()
    skipped = 0

    # Phases 1 then 2, in the plan's structural order.
    for desired in plan.all_subscribers:
        if desired.suppressed and desired.subscribable:
            raise SuppressionViolationError(
                f"write-time invariant check: {desired.email} is suppressed but "
                "planned subscribable; aborting before the write"
            )
        action = _subscriber_action(desired, current)
        if action == "create":
            client.create_subscriber(desired, list_id)
        elif action == "update":
            client.update_subscriber(current[desired.email.lower()], desired, list_id)
        else:
            skipped += 1
            continue
        written[action] += 1

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

    return ExecutionSummary(list_id=list_id, written=dict(written), skipped_unchanged=skipped)
