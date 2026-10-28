"""RC1-574: reconciliation against a destination that already held data.
The first run journals the destination as it found it; reconcile reads that
baseline back, leaves pre-existing members out of membership, and accepts a
kept conflict ONLY when the baseline proves the destination protected the
address first (ADR-0019). Suppression integrity never consults the baseline,
and a protection the baseline shows is never allowed to come undone.
FakeTarget/ListAwareTarget throughout; journals live under tmp_path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from executor.engine import execute
from executor.journal import ExecutionJournal, find_baseline
from executor.plan import MigrationPlan, build_plan
from executor.reconcile import reconcile, render_markdown
from executor.target import TargetSubscriber
from ingest.mailchimp import load_bundle
from mapping.gate import approve, load_approved
from seeding.build_corpora import AUDIENCE_A, AUDIENCE_B, SYNTHETIC
from specs.model import MappingSpec
from tests.fake_target import FakeTarget, ListAwareTarget

APPROVED = Path("specs/approved/mailchimp_to_listmonk.yaml")
STRICTER, LAXER, EXTRAS = 300, 200, 150


def events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def migrate(plan: MigrationPlan, target: FakeTarget, path: Path):
    """As executor.run does it: run_start (destination identity) first."""
    journal = ExecutionJournal(path)
    journal.run_start(
        target=plan.target, spec={}, source={},
        destination=target.describe_destination(), list_name=plan.list_name,
    )
    summary = execute(plan, target, journal)
    journal.run_complete(summary)
    journal.close()
    return summary, journal.run_id


def seed(target: FakeTarget, email: str, name: str, status: str, subscription: str,
         attribs: dict) -> None:
    target.subscribers[email.lower()] = TargetSubscriber(
        id=target._next_id, email=email, name=name, status=status,
        subscription=subscription, attribs=dict(attribs),
    )
    target._next_id += 1


def set_state(target: FakeTarget, email: str, status: str, subscription: str) -> None:
    record = target.subscribers[email.lower()]
    target.subscribers[email.lower()] = record.model_copy(
        update={"status": status, "subscription": subscription}
    )


@pytest.fixture(scope="module")
def spec():
    return load_approved(APPROVED)


@pytest.fixture(scope="module")
def bundle():
    return load_bundle(Path("fixtures/mailchimp_export/audience"), Path("fixtures/api_supplement"))


@pytest.fixture()
def prepopulated(spec, bundle):
    """A live customer list: some planned addresses already protected, some
    still mailable that the source suppresses, and contacts the source has
    never heard of."""
    plan = build_plan(bundle, spec)
    target = FakeTarget()
    target.ensure_list(plan.list_name)
    stricter = plan.subscribers[:STRICTER]
    laxer = plan.suppressions[:LAXER]
    for d in stricter:
        seed(target, d.email, d.name, "blocklisted", "unsubscribed", d.attribs)
    for d in laxer:
        seed(target, d.email, d.name, "enabled", "confirmed", d.attribs)
    extras = [f"existing{i}@customer.example" for i in range(EXTRAS)]
    for i, email in enumerate(extras):
        seed(target, email, f"Existing {i}", "enabled", "confirmed", {"crm_id": i})
    return plan, target, stricter, laxer, extras


# --- the baseline makes a correct migration into a live list PASS ------------------------


def test_correct_migration_into_a_live_list_passes(prepopulated, spec, bundle, tmp_path):
    plan, target, stricter, _, _ = prepopulated
    path = tmp_path / "journal.jsonl"
    _, run_id = migrate(plan, target, path)

    report = reconcile(bundle, spec, target, path)

    assert report.passed, render_markdown(report)
    assert report.baseline_run == run_id
    assert report.membership.preexisting == EXTRAS
    assert report.membership.unexpected == []
    assert {d.email for d in report.deviations} == {d.email for d in stricter}
    assert all(d.before == "blocklisted/unsubscribed" for d in report.deviations)
    assert report.suppression.pct == 100.0 and report.downgrades == []
    assert all(c.ok for c in report.consent_counts)
    assert all(s.ok for s in report.segments)  # extras counted where they belong


def test_without_the_journal_the_same_target_fails_as_before(prepopulated, spec, bundle):
    plan, target, _, _, extras = prepopulated
    execute(plan, target)
    report = reconcile(bundle, spec, target)
    assert not report.passed
    assert set(report.membership.unexpected) == {e.lower() for e in extras}
    assert "destination assumed empty" in report.baseline_note


def test_the_report_says_what_was_accepted(prepopulated, spec, bundle, tmp_path):
    plan, target, stricter, _, _ = prepopulated
    path = tmp_path / "journal.jsonl"
    migrate(plan, target, path)
    markdown = render_markdown(reconcile(bundle, spec, target, path))
    assert "Overall: **PASS**" in markdown
    assert f"{EXTRAS:,} pre-existing member(s) outside the plan" in markdown
    assert f"{STRICTER:,} planned address(es) the destination already protected" in markdown
    assert "Every address the baseline shows protected is still protected." in markdown


# --- the baseline is the FIRST look, and only a pristine one -----------------------------


def test_only_the_first_run_journals_a_baseline(prepopulated, tmp_path):
    plan, target, _, _, _ = prepopulated
    path = tmp_path / "journal.jsonl"
    _, first = migrate(plan, target, path)
    migrate(plan, target, path)  # a rerun sees the migration's own records

    baselines = [e for e in events(path) if e["event"] == "baseline"]
    assert [b["run_id"] for b in baselines] == [first]
    found = find_baseline(path, target.describe_destination(), plan.list_name)
    assert found.baseline.run_id == first
    assert len(found.baseline.members) == STRICTER + LAXER + EXTRAS


def test_a_list_migrated_before_baselining_never_gets_one(prepopulated, spec, bundle, tmp_path):
    plan, target, _, _, _ = prepopulated
    path = tmp_path / "journal.jsonl"
    legacy = {"run_id": "legacy", "at": "2026-10-01T00:00:00Z"}
    path.write_text(
        json.dumps({**legacy, "event": "run_start", "list_name": plan.list_name,
                    "destination": target.describe_destination()}) + "\n"
        + json.dumps({**legacy, "event": "write", "phase": "2-subscribers",
                      "action": "create", "key": "someone@example.com"}) + "\n"
    )
    migrate(plan, target, path)

    assert [e for e in events(path) if e["event"] == "baseline"] == []
    report = reconcile(bundle, spec, target, path)
    assert report.baseline_run is None and not report.passed
    assert "writes to this list before any baseline" in report.baseline_note


def test_a_baseline_of_another_destination_or_list_does_not_apply(prepopulated, tmp_path):
    plan, target, _, _, _ = prepopulated
    path = tmp_path / "journal.jsonl"
    migrate(plan, target, path)
    other_host = {**target.describe_destination(), "host": "elsewhere"}
    assert find_baseline(path, other_host, plan.list_name).baseline is None
    assert find_baseline(path, target.describe_destination(), "Other list").baseline is None


def test_a_recreated_list_does_not_inherit_the_old_baseline(prepopulated, spec, bundle, tmp_path):
    plan, target, _, _, _ = prepopulated
    path = tmp_path / "journal.jsonl"
    migrate(plan, target, path)
    target.lists[plan.list_name] = 99  # same name, new list

    report = reconcile(bundle, spec, target, path)
    assert report.baseline_run is None
    assert "recreated" in report.baseline_note


# --- what the baseline must never excuse ---------------------------------------------------


def test_strictness_the_migration_made_itself_still_fails(prepopulated, spec, bundle, tmp_path):
    plan, target, stricter, _, _ = prepopulated
    path = tmp_path / "journal.jsonl"
    migrate(plan, target, path)
    wronged = plan.subscribers[STRICTER]  # planned subscribable, not in the baseline
    set_state(target, wronged.email, "blocklisted", "unsubscribed")

    report = reconcile(bundle, spec, target, path)
    assert not report.passed
    assert wronged.email not in {d.email for d in report.deviations}
    assert not all(c.ok for c in report.consent_counts)


def test_a_downgraded_protection_fails_even_when_it_matches_the_plan(
    prepopulated, spec, bundle, tmp_path
):
    plan, target, stricter, _, _ = prepopulated
    path = tmp_path / "journal.jsonl"
    migrate(plan, target, path)
    undone = stricter[0]  # the plan says subscribed; the destination had said no
    set_state(target, undone.email, undone.status, undone.subscription)

    report = reconcile(bundle, spec, target, path)
    assert report.downgrades == [undone.email]
    assert not report.passed
    assert "DOWNGRADED" in render_markdown(report)


def test_suppression_integrity_never_consults_the_baseline(prepopulated, spec, bundle, tmp_path):
    plan, target, _, laxer, _ = prepopulated
    path = tmp_path / "journal.jsonl"
    migrate(plan, target, path)
    reverted = laxer[0]  # mailable in the baseline, suppressed by the plan
    set_state(target, reverted.email, "enabled", "confirmed")

    report = reconcile(bundle, spec, target, path)
    assert report.suppression.violations == [reverted.email]
    assert not report.passed


def test_a_contact_added_after_the_baseline_is_unexpected(prepopulated, spec, bundle, tmp_path):
    plan, target, _, _, _ = prepopulated
    path = tmp_path / "journal.jsonl"
    migrate(plan, target, path)
    seed(target, "latecomer@customer.example", "Late", "enabled", "confirmed", {})

    report = reconcile(bundle, spec, target, path)
    assert report.membership.unexpected == ["latecomer@customer.example"]
    assert not report.passed


# --- the cross-audience case: protection found on ANOTHER list ---------------------------


def audience_spec(audience: str) -> MappingSpec:
    data = load_approved(APPROVED).model_dump(mode="json")
    data.update(approval=None, status="draft", source_audience=audience)
    return approve(MappingSpec.model_validate(data), approved_by="test reviewer")


def test_second_audience_reconciles_with_its_attached_suppressions(tmp_path):
    multi = SYNTHETIC / "multi_audience"
    truth = json.loads((multi / "ground_truth.json").read_text())
    spec_a, spec_b = audience_spec(AUDIENCE_A), audience_spec(AUDIENCE_B)
    bundle_a = load_bundle(multi / "audience_a", multi / "api_supplement")
    bundle_b = load_bundle(multi / "audience_b", multi / "api_supplement")
    target = ListAwareTarget()
    path = tmp_path / "journal.jsonl"
    migrate(build_plan(bundle_a, spec_a), target, path)
    migrate(build_plan(bundle_b, spec_b), target, path)

    report = reconcile(bundle_b, spec_b, target, path)

    assert report.passed, render_markdown(report)
    assert {d.email for d in report.deviations} == set(truth["overlap_consent_conflicts"])
    assert all(d.before == "blocklisted/(another list)" for d in report.deviations)
    assert all(d.landed == "blocklisted/unsubscribed" for d in report.deviations)
    assert report.membership.preexisting == 0  # B's list was new

