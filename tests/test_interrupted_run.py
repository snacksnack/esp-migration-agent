"""RC1-570: an interrupted run resumes by re-running. The engine makes no
checkpoint on purpose - every write is an upsert, so the recovery procedure
for a run that died mid-write is the same command again: the second pass
writes only what is missing and a third pass proves convergence."""

from __future__ import annotations

import pytest

from executor.engine import compute_diff, execute
from executor.plan import DesiredSubscriber, MigrationPlan, PlannedCampaign
from executor.target import TargetError
from tests.fake_target import FakeTarget


class DyingTarget(FakeTarget):
    """Healthy until `die_after` writes have landed, then every further write
    fails like a lost connection. Setting die_after back to None heals it."""

    def __init__(self, die_after: int):
        super().__init__()
        self.die_after: int | None = die_after

    def _store(self, desired: DesiredSubscriber, sub_id: int) -> None:
        if self.die_after is not None and self.write_calls >= self.die_after:
            raise TargetError("create subscriber: connection error: connection lost")
        super()._store(desired, sub_id)


def subscriber(i: int, *, suppressed: bool) -> DesiredSubscriber:
    return DesiredSubscriber(
        email=f"{'sup' if suppressed else 'sub'}{i}@example.com",
        name=f"Sub {i}",
        status="blocklisted" if suppressed else "enabled",
        subscription="unsubscribed" if suppressed else "confirmed",
        subscribable=not suppressed,
        suppressed=suppressed,
        attribs={},
    )


@pytest.fixture()
def plan() -> MigrationPlan:
    return MigrationPlan(
        source="mailchimp",
        target="listmonk",
        list_name="Migrated",
        suppressions=[subscriber(i, suppressed=True) for i in range(4)],
        subscribers=[subscriber(i, suppressed=False) for i in range(8)],
        campaigns=[PlannedCampaign(name="Welcome", subject="Hi", body="<p>Hi</p>")],
        segments=[],
    )


def test_interrupted_run_resumes_and_converges(plan):
    target = DyingTarget(die_after=6)  # dies mid-phase-2, after the suppressions
    with pytest.raises(TargetError, match="connection lost"):
        execute(plan, target)
    assert target.write_calls == 6  # a partial run: 4 suppressions + 2 subscribers
    assert len(target.subscribers) == 6

    target.die_after = None  # the outage ends; the operator re-runs
    summary = execute(plan, target)
    assert summary.written == {"create": 7}  # only the missing 6 subscribers + campaign
    assert summary.skipped_unchanged == 6  # the landed writes were not repeated
    assert len(target.subscribers) == 12
    assert len(target.campaigns) == 1

    third = execute(plan, target)  # and the third pass proves convergence
    assert third.written == {}
    diff = compute_diff(plan, target.current_subscribers(0), target.current_campaigns())
    assert not diff.writes


def test_interruption_never_reorders_the_phases(plan):
    # Die on the very first phase-2 write: everything landed so far must be
    # a suppression - the invariant's ordering survives the crash.
    target = DyingTarget(die_after=4)
    with pytest.raises(TargetError):
        execute(plan, target)
    assert all(s.status == "blocklisted" for s in target.subscribers.values())
    assert target.campaigns == {}
