"""RC1-572: the engine against targets that are not empty and audiences that
are not alone. Two limits are pinned here on purpose (ADR-0017):

- Every audience plans into the same destination list name, so a second
  audience lands on top of the first. The suppression invariant still holds
  across audiences (an address unsubscribed in A stays protected when B
  says subscribed - ADR-0013), and reconciliation FAILS, naming exactly the
  other audience's addresses: loud, never silent.
- Reconciliation assumes the destination list started empty. Against a
  pre-populated target it fails, naming exactly the pre-existing addresses
  and the kept conflicts - again precise, never silent. Baselining
  pre-existing members is follow-up work.

FakeTarget throughout; fixture scale where it matters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from executor.engine import execute
from executor.plan import DesiredSubscriber, build_plan
from executor.reconcile import build_reconciliation
from executor.target import TargetSubscriber
from ingest.mailchimp import load_bundle
from mapping.gate import load_approved
from seeding.build_corpora import SYNTHETIC
from tests.fake_target import FakeTarget

APPROVED = Path("specs/approved/mailchimp_to_listmonk.yaml")
MULTI = SYNTHETIC / "multi_audience"


@pytest.fixture(scope="module")
def spec():
    return load_approved(APPROVED)


def _seed(target: FakeTarget, d: DesiredSubscriber, status: str, subscription: str,
          attribs: dict | None = None) -> None:
    target.subscribers[d.email.lower()] = TargetSubscriber(
        id=target._next_id, email=d.email, name=d.name, status=status,
        subscription=subscription, attribs=dict(d.attribs if attribs is None else attribs),
    )
    target._next_id += 1


# --- two audiences, one destination ------------------------------------------------


@pytest.fixture(scope="module")
def audiences(spec):
    truth = json.loads((MULTI / "ground_truth.json").read_text())
    bundle_a = load_bundle(MULTI / "audience_a", MULTI / "api_supplement")
    bundle_b = load_bundle(MULTI / "audience_b", MULTI / "api_supplement")
    return truth, bundle_a, bundle_b, build_plan(bundle_a, spec), build_plan(bundle_b, spec)


def test_every_audience_plans_into_the_same_list(audiences):
    *_, plan_a, plan_b = audiences
    assert plan_a.list_name == plan_b.list_name  # the limit, pinned


def test_second_audience_never_downgrades_the_first_audiences_suppressions(audiences):
    truth, _, _, plan_a, plan_b = audiences
    conflicted = set(truth["overlap_consent_conflicts"])
    target = FakeTarget()
    execute(plan_a, target)
    summary_b = execute(plan_b, target)

    # Unsubscribed in A, subscribed in B: kept protected and reported.
    assert set(summary_b.conflicts) == conflicted
    for email in conflicted:
        assert target.subscribers[email].status == "blocklisted"
    # Same consent in both: B finds them already converged, writes nothing.
    for email in truth["overlap_same_consent"]:
        assert target.subscribers[email].status == "enabled"


def test_reconciling_the_second_audience_names_the_first(audiences, spec):
    truth, bundle_a, bundle_b, plan_a, plan_b = audiences
    target = FakeTarget()
    execute(plan_a, target)
    summary = execute(plan_b, target)
    report = build_reconciliation(plan_b, spec, bundle_b, target, summary.list_id)

    a_only = {m.email for m in bundle_a.all_members} - {m.email for m in bundle_b.all_members}
    assert not report.passed
    assert set(report.membership.unexpected) == a_only
    assert report.membership.missing == []
    assert report.suppression.violations == []  # B's own suppressions all landed


# --- a destination that already holds data -------------------------------------------


STRICTER, LAXER, EXTRAS = 300, 200, 150


@pytest.fixture()  # function scope: each test executes against a fresh target
def prepopulated(spec):
    bundle = load_bundle(
        Path("fixtures/mailchimp_export/audience"), Path("fixtures/api_supplement")
    )
    plan = build_plan(bundle, spec)
    target = FakeTarget()
    target.ensure_list(plan.list_name)

    stricter = plan.subscribers[:STRICTER]  # destination already protects them
    laxer = plan.suppressions[:LAXER]  # destination still mails them
    for d in stricter:
        _seed(target, d, "blocklisted", "unsubscribed")
    for d in laxer:
        _seed(target, d, "enabled", "confirmed")
    extras = [
        DesiredSubscriber(
            email=f"existing{i}@customer.example", name=f"Existing {i}",
            status="enabled", subscription="confirmed", subscribable=True,
            suppressed=False, attribs={},
        )
        for i in range(EXTRAS)
    ]
    for i, d in enumerate(extras):
        _seed(target, d, "enabled", "confirmed", attribs={"crm_id": i})
    return bundle, plan, target, stricter, laxer, extras


def test_prepopulated_target_keeps_protection_and_enforces_suppression(prepopulated):
    _, plan, target, stricter, laxer, extras = prepopulated
    summary = execute(plan, target)

    assert summary.conflicts == [d.email for d in stricter]  # kept, in plan order
    assert summary.written == {
        "create": len(plan.all_subscribers) - STRICTER - LAXER + len(plan.campaigns),
        "update": LAXER,  # suppressing always writes (ADR-0013)
    }
    for d in plan.suppressions:
        assert target.subscribers[d.email.lower()].status == "blocklisted"
    for d in stricter:
        assert target.subscribers[d.email.lower()].status == "blocklisted"
    for i, d in enumerate(extras):  # not the executor's to touch
        assert target.subscribers[d.email].attribs == {"crm_id": i}

    rerun = execute(plan, target)  # converged, conflicts re-reported every run
    assert rerun.written == {}
    assert len(rerun.conflicts) == STRICTER


def test_reconciliation_against_a_prepopulated_target_fails_precisely(prepopulated, spec):
    bundle, plan, target, stricter, _, extras = prepopulated
    execute(plan, target)
    report = build_reconciliation(plan, spec, bundle, target, target.lists[plan.list_name])

    assert not report.passed
    assert report.suppression.violations == []  # the invariant held at scale
    assert report.membership.missing == []
    assert set(report.membership.unexpected) == {d.email for d in extras}
    kept = {d.email for d in stricter}
    assert set(report.checksums.mismatches) <= kept  # only the kept conflicts differ
