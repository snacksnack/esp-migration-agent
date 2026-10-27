"""RC1-568 (ADR-0013): the executor never downgrades stricter destination
consent - a contact who unsubscribed at the destination is kept unsubscribed,
reported as a conflict, and the suppression direction still always writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from executor.engine import compute_diff, execute, render_diff
from executor.plan import build_plan, target_state_subscribable
from executor.target import TargetSubscriber
from ingest.mailchimp import load_bundle
from mapping.gate import load_approved
from tests.fake_target import FakeTarget

AUDIENCE = Path("fixtures/mailchimp_export/audience")
SUPPLEMENT = Path("fixtures/api_supplement")
APPROVED = Path("specs/approved/mailchimp_to_listmonk.yaml")


@pytest.fixture(scope="module")
def plan():
    bundle = load_bundle(AUDIENCE, SUPPLEMENT)
    return build_plan(bundle, load_approved(APPROVED))


def _seed(target: FakeTarget, email: str, status: str, subscription: str) -> None:
    list_id = target.ensure_list("Imported audience (mailchimp)")
    target.subscribers[email.lower()] = TargetSubscriber(
        id=999_001, email=email, name="Pre Existing",
        status=status, subscription=subscription, attribs={},
    )
    assert list_id


def test_target_state_subscribable_knows_the_dialect():
    assert not target_state_subscribable("listmonk", "enabled", "unsubscribed")
    assert not target_state_subscribable("listmonk", "blocklisted", "unsubscribed")
    assert target_state_subscribable("listmonk", "enabled", "confirmed")
    # Unknown states are unprotected: only a state the dialect knows is
    # stricter may veto a write.
    assert target_state_subscribable("listmonk", "weird", "state")


def test_destination_unsubscribe_is_kept_and_reported(plan):
    victim = next(d for d in plan.subscribers if d.subscribable)
    target = FakeTarget()
    _seed(target, victim.email, "enabled", "unsubscribed")

    diff = compute_diff(plan, target.current_subscribers(1), target.current_campaigns())
    conflict_keys = [e.key for e in diff.entries if e.action == "conflict"]
    assert conflict_keys == [victim.email]
    assert "never downgrades protection" in render_diff(diff)
    assert all(e.key != victim.email for e in diff.writes)

    summary = execute(plan, target)
    assert summary.conflicts == [victim.email]
    landed = target.subscribers[victim.email.lower()]
    assert (landed.status, landed.subscription) == ("enabled", "unsubscribed")


def test_suppression_direction_always_writes(plan):
    # The protection is one-way: a plan that SUPPRESSES an address existing
    # as subscribed in the target must still write it non-subscribable.
    suppressed = plan.suppressions[0]
    target = FakeTarget()
    _seed(target, suppressed.email, "enabled", "confirmed")

    summary = execute(plan, target)
    assert suppressed.email not in summary.conflicts
    landed = target.subscribers[suppressed.email.lower()]
    assert not target_state_subscribable("listmonk", landed.status, landed.subscription)


def test_fresh_target_has_no_conflicts(plan):
    summary = execute(plan, FakeTarget())
    assert summary.conflicts == []
