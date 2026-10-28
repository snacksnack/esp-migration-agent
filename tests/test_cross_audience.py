"""RC1-573: two audiences of one account, migrated one at a time, each into
its own destination list - against a fake target that behaves like the real
ones were PROBED to behave (ADR-0018): contacts and their status are global,
subscriptions are per list, and creating an address that exists anywhere
fails (Listmonk answers 409 "E-mail already exists")."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from executor.engine import compute_diff, execute
from executor.plan import DesiredSubscriber, PlanError, build_plan, destination_list_name
from executor.reconcile import build_reconciliation
from executor.target import TargetError, TargetSubscriber
from ingest.mailchimp import IngestError, load_bundle, resolve_audience
from mapping.gate import approve, load_approved
from seeding.build_corpora import AUDIENCE_A, AUDIENCE_B, SYNTHETIC
from specs.model import MappingSpec
from tests.fake_target import FakeTarget

APPROVED = Path("specs/approved/mailchimp_to_listmonk.yaml")
MULTI = SYNTHETIC / "multi_audience"


class ListAwareTarget(FakeTarget):
    """Global records (status, name, attribs) plus per-list subscriptions."""

    def __init__(self) -> None:
        super().__init__()
        self.members: dict[int, dict[str, str]] = {}  # list id -> email -> subscription

    def ensure_list(self, name: str) -> int:
        list_id = super().ensure_list(name)
        self.members.setdefault(list_id, {})
        return list_id

    def _view(self, key: str, list_id: int | None) -> TargetSubscriber:
        record = self.subscribers[key]
        return record.model_copy(
            update={
                "subscription": self.members.get(list_id, {}).get(key, ""),
                "list_ids": sorted(lid for lid, m in self.members.items() if key in m),
            },
            deep=True,
        )

    def current_subscribers(self, list_id: int) -> dict[str, TargetSubscriber]:
        return {key: self._view(key, list_id) for key in self.members.get(list_id, {})}

    def lookup_subscribers(self, emails: list[str]) -> dict[str, TargetSubscriber]:
        return {
            e.lower(): self._view(e.lower(), None)
            for e in emails if e.lower() in self.subscribers
        }

    def create_subscriber(self, desired: DesiredSubscriber, list_id: int) -> None:
        if desired.email.lower() in self.subscribers:
            raise TargetError(f"create subscriber {desired.email}: HTTP 409 E-mail already exists")
        super().create_subscriber(desired, list_id)
        self.members[list_id][desired.email.lower()] = desired.subscription

    def update_subscriber(self, existing, desired, list_id) -> None:
        super().update_subscriber(existing, desired, list_id)
        self.members[list_id][desired.email.lower()] = desired.subscription

    def attach_subscriber(self, existing: TargetSubscriber, list_id: int) -> None:
        self.write_calls += 1
        self.members[list_id][existing.email.lower()] = "unsubscribed"


def spec_for(audience: str | None) -> MappingSpec:
    data = load_approved(APPROVED).model_dump(mode="json")
    data.update(approval=None, status="draft", source_audience=audience)
    return approve(MappingSpec.model_validate(data), approved_by="test reviewer")


@pytest.fixture(scope="module")
def truth() -> dict:
    return json.loads((MULTI / "ground_truth.json").read_text())


@pytest.fixture(scope="module")
def bundles():
    return (
        load_bundle(MULTI / "audience_a", MULTI / "api_supplement"),
        load_bundle(MULTI / "audience_b", MULTI / "api_supplement"),
    )


@pytest.fixture(scope="module")
def plans(bundles):
    bundle_a, bundle_b = bundles
    return build_plan(bundle_a, spec_for(AUDIENCE_A)), build_plan(bundle_b, spec_for(AUDIENCE_B))


# --- one audience per spec, per plan, per list -----------------------------------------


def test_each_audience_gets_its_own_list_and_only_its_campaigns(plans):
    plan_a, plan_b = plans
    assert plan_a.list_name == f"{AUDIENCE_A} (imported from mailchimp)"
    assert plan_b.list_name == f"{AUDIENCE_B} (imported from mailchimp)"
    assert [c.name for c in plan_a.campaigns] == ["Audience A news"]
    assert [c.name for c in plan_b.campaigns] == ["Audience B news"]


def test_spec_naming_no_audience_is_refused_when_the_supplement_spans_several(bundles):
    with pytest.raises(PlanError, match="spans 2 audiences"):
        build_plan(bundles[0], spec_for(None))


def test_spec_for_an_audience_the_supplement_never_mentions_is_refused(bundles):
    with pytest.raises(PlanError, match="wrong export"):
        build_plan(bundles[0], spec_for("Somebody else's list"))


def test_legacy_single_audience_spec_keeps_its_list_name():
    # The committed approval predates RC1-573: same list, same e2e target.
    assert destination_list_name(load_approved(APPROVED)) == "Imported audience (mailchimp)"


def test_propose_resolves_the_audience_or_refuses_to_guess(bundles):
    bundle_a, _ = bundles
    assert resolve_audience(bundle_a, AUDIENCE_A) == AUDIENCE_A
    with pytest.raises(IngestError, match="name the one this export is with --audience"):
        resolve_audience(bundle_a, None)
    with pytest.raises(IngestError, match="not one the supplement's campaigns target"):
        resolve_audience(bundle_a, "Typo customers")
    genuine = load_bundle(
        Path("fixtures/mailchimp_export/audience"), Path("fixtures/api_supplement")
    )
    assert resolve_audience(genuine, None) == "hirereidcollins"  # one audience: inferred


def test_the_audience_is_bound_by_the_approval():
    spec = spec_for(AUDIENCE_A)
    tampered = spec.model_copy(update={"source_audience": AUDIENCE_B})
    assert tampered.content_sha256() != spec.approval.content_sha256


# --- the second audience never downgrades the first --------------------------------------


@pytest.fixture()
def migrated(plans):
    plan_a, plan_b = plans
    target = ListAwareTarget()
    execute(plan_a, target)
    summary_b = execute(plan_b, target)
    return target, summary_b


def test_second_audience_attaches_the_first_audiences_suppressions(migrated, truth, plans):
    target, summary_b = migrated
    conflicted = set(truth["overlap_consent_conflicts"])
    list_b = target.lists[plans[1].list_name]

    assert set(summary_b.conflicts) == conflicted
    assert summary_b.written.get("attach") == len(conflicted)
    for email in conflicted:
        assert target.subscribers[email].status == "blocklisted"  # never downgraded
        assert target.members[list_b][email] == "unsubscribed"  # on B, unmailable


def test_shared_subscribers_join_the_second_list_and_keep_the_first(migrated, truth, plans):
    target, _ = migrated
    list_a, list_b = (target.lists[p.list_name] for p in plans)
    for email in truth["overlap_same_consent"]:
        assert target.members[list_a][email] == "confirmed"  # A's membership kept
        assert target.members[list_b][email] == "confirmed"
        assert target.subscribers[email].status == "enabled"


def test_cross_audience_rerun_converges(migrated, plans):
    target, _ = migrated
    rerun = execute(plans[1], target)
    assert rerun.written == {}
    assert len(rerun.conflicts) == 15  # now members: plain conflicts, re-reported


def test_dry_run_shows_the_attaches_the_real_run_makes(plans, truth):
    plan_a, plan_b = plans
    target = ListAwareTarget()
    execute(plan_a, target)
    list_b = target.ensure_list(plan_b.list_name)
    current = target.current_subscribers(list_b)
    elsewhere = target.lookup_subscribers(
        [d.email for d in plan_b.all_subscribers if d.email.lower() not in current]
    )
    diff = compute_diff(plan_b, current, target.current_campaigns(), elsewhere)
    attaches = {e.key for e in diff.entries if e.action == "attach"}
    assert attaches == set(truth["overlap_consent_conflicts"])
    assert diff.writes  # pending attaches are writes: not "already converged"


def test_a_later_audiences_suppression_applies_account_wide(plans, truth):
    """Reverse order: B (subscribed) first, then A (unsubscribed). Listmonk's
    blocklist is global, so A's suppression stops mail on B's list too - the
    safe direction, and B's membership is preserved rather than dropped. A
    spec mapping unsubscribed to the per-list 'unsubscribed' label instead
    would keep B mailable; that is the reviewer's call (ADR-0018)."""
    plan_a, plan_b = plans
    target = ListAwareTarget()
    execute(plan_b, target)
    summary_a = execute(plan_a, target)
    list_b = target.lists[plan_b.list_name]
    for email in truth["overlap_consent_conflicts"]:
        assert target.subscribers[email].status == "blocklisted"
        assert email in target.members[list_b]  # not silently removed from B
    assert summary_a.conflicts == []  # suppressing is never a conflict


def test_reconciling_the_second_audience_fails_only_on_the_kept_conflicts(
    migrated, plans, bundles, truth
):
    target, summary_b = migrated
    report = build_reconciliation(
        plans[1], spec_for(AUDIENCE_B), bundles[1], target, summary_b.list_id
    )
    assert report.membership.missing == []
    assert report.membership.unexpected == []  # A's members are on A's list now
    assert report.suppression.violations == []
    # What still fails is the kept protection itself - expected deviations
    # reconciliation does not yet know about (RC1-574).
    assert set(report.checksums.mismatches) <= set(truth["overlap_consent_conflicts"])
