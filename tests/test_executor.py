"""RC1-546 acceptance: plan building, THE suppression invariant (including a
hostile approved spec), dry-run diff, execution, and idempotent convergence -
all against the committed fixture and the in-memory fake target."""

from __future__ import annotations

from pathlib import Path

import pytest

from executor.engine import compute_diff, execute, render_diff
from executor.plan import (
    PlanError,
    SuppressionViolationError,
    build_plan,
    translate_merge_tags,
)
from ingest.mailchimp import load_bundle
from mapping.gate import approve, load_approved
from specs.model import MappingSpec
from tests.fake_target import FakeTarget

AUDIENCE = Path("fixtures/mailchimp_export/audience")
SUPPLEMENT = Path("fixtures/api_supplement")
APPROVED = Path("specs/approved/mailchimp_to_listmonk.yaml")


@pytest.fixture(scope="module")
def bundle():
    return load_bundle(AUDIENCE, SUPPLEMENT)


@pytest.fixture(scope="module")
def spec():
    return load_approved(APPROVED)


@pytest.fixture(scope="module")
def plan(bundle, spec):
    return build_plan(bundle, spec)


# --- plan building -----------------------------------------------------------


def test_plan_covers_every_exported_member(plan):
    assert len(plan.suppressions) == 411  # the unsubscribed bucket
    assert len(plan.subscribers) == 3844  # subscribed 3756 + nonsubscribed 88
    assert len(plan.all_subscribers) == 4255


def test_suppressions_are_phase_one_and_non_subscribable(plan):
    assert plan.all_subscribers[: len(plan.suppressions)] == plan.suppressions
    assert all(d.suppressed and not d.subscribable for d in plan.suppressions)
    assert all(d.status == "blocklisted" for d in plan.suppressions)


def test_transforms_applied(plan):
    by_email = {d.email: d for d in plan.all_subscribers}
    padded = [
        d for d in by_email.values()
        if len(str(d.attribs.get("zip", ""))) == 5 and str(d.attribs["zip"]).startswith("0")
    ]
    assert padded, "zero_pad_us_zip produced no padded zips"
    assert all("mc_member_rating" in d.attribs for d in plan.all_subscribers)


def test_plan_requires_an_approved_spec(bundle):
    draft = MappingSpec.from_yaml(
        Path("specs/drafts/mailchimp_to_listmonk.resolved.yaml").read_text()
    )
    with pytest.raises(PlanError, match="approved"):
        build_plan(bundle, draft)


def test_campaigns_fully_translated(plan):
    assert len(plan.campaigns) == 3
    for c in plan.campaigns:
        assert "*|" not in c.body
        assert "{{" in c.body  # target syntax present


def test_untranslated_merge_tag_fails_loudly():
    with pytest.raises(PlanError, match="MYSTERY"):
        translate_merge_tags("<p>*|MYSTERY|*</p>", {}, context="campaign 'x'")


# --- THE invariant: a hostile approved spec cannot violate it ----------------


def _hostile_spec(spec: MappingSpec) -> MappingSpec:
    """An approved spec whose consent mapping tries to resubscribe the
    suppressed bucket. The gate approves it (it is fully reviewed - the gate
    checks resolution, not semantics); the plan builder must refuse."""
    data = spec.model_dump(mode="json")
    data["approval"] = None
    data["status"] = "draft"
    for m in data["consent_mappings"]:
        if m["source_status"] == "unsubscribed":
            m["status"] = "edited"
            m["target_status"] = "enabled/confirmed"  # the hostile edit
    return approve(MappingSpec.model_validate(data), approved_by="hostile reviewer")


def test_hostile_approved_spec_cannot_land_suppressed_subscribable(bundle, spec):
    hostile = _hostile_spec(spec)
    target = FakeTarget()
    with pytest.raises(SuppressionViolationError, match="suppressed"):
        plan = build_plan(bundle, hostile)  # must raise here, before any plan exists
        execute(plan, target)  # never reached
    assert target.write_calls == 0, "a write happened despite the violation"
    assert target.subscribers == {}


def test_write_loop_rechecks_the_invariant(plan):
    # Belt and braces: even a hand-tampered plan object cannot get a
    # suppressed record written subscribable.
    tampered = plan.model_copy(deep=True)
    tampered.suppressions[0].subscribable = True
    target = FakeTarget()
    with pytest.raises(SuppressionViolationError, match="write-time"):
        execute(tampered, target)
    assert target.write_calls == 0


# --- dry-run diff and execution ----------------------------------------------


def test_dry_run_diff_renders_before_any_write(plan):
    target = FakeTarget()
    diff = compute_diff(plan, target.current_subscribers(0), target.current_campaigns())
    text = render_diff(diff)
    assert "1-suppressions: create: 411" in text
    assert "2-subscribers: create: 3,844" in text
    assert "3-campaigns: create: 3" in text
    assert target.write_calls == 0


def test_execute_migrates_full_fixture(plan):
    target = FakeTarget()
    summary = execute(plan, target)
    assert summary.written == {"create": 4258}  # 4,255 subscribers + 3 campaigns
    assert len(target.subscribers) == 4255
    assert len(target.campaigns) == 3
    suppressed_landed = [
        target.subscribers[d.email.lower()] for d in plan.suppressions
    ]
    assert all(s.status == "blocklisted" for s in suppressed_landed)


def test_second_run_converges_to_zero_writes(plan):
    target = FakeTarget()
    execute(plan, target)
    writes_after_first = target.write_calls
    summary = execute(plan, target)
    assert target.write_calls == writes_after_first
    assert summary.written == {}
    assert summary.skipped_unchanged == 4258


def test_partial_migration_converges_without_duplicates(plan):
    # Simulate a crashed first run: only half the records landed.
    target = FakeTarget()
    half = plan.model_copy(
        update={
            "suppressions": plan.suppressions[:200],
            "subscribers": plan.subscribers[:1000],
            "campaigns": [],
        }
    )
    execute(half, target)
    execute(plan, target)  # full re-run
    assert len(target.subscribers) == 4255  # no duplicates, fully converged
    diff = compute_diff(plan, target.current_subscribers(0), target.current_campaigns())
    assert not diff.writes
