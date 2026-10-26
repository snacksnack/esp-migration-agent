"""RC1-546: reconciliation math (against the fake target) and the cutover
plan artifact."""

from __future__ import annotations

from pathlib import Path

import pytest

from executor.cutover import render_cutover_plan
from executor.engine import execute
from executor.plan import build_plan
from executor.reconcile import (
    QueryNotEvaluatable,
    evaluate_query,
    reconcile,
    render_markdown,
)
from ingest.mailchimp import load_bundle
from mapping.gate import load_approved
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


@pytest.fixture()
def migrated(bundle, spec):
    target = FakeTarget()
    execute(build_plan(bundle, spec), target)
    return target


# --- query evaluator ----------------------------------------------------------


def test_evaluate_like_and_int_queries():
    attribs = {"zip": "11215", "last_purchase": "2026-01-15", "mc_member_rating": 2}
    assert evaluate_query("subscribers.attribs->>'zip' LIKE '112%'", attribs)
    assert not evaluate_query("subscribers.attribs->>'zip' LIKE '100%'", attribs)
    assert evaluate_query("subscribers.attribs->>'last_purchase' LIKE '%2026%'", attribs)
    assert not evaluate_query("(subscribers.attribs->>'mc_member_rating')::int > 3", attribs)
    assert evaluate_query(
        "subscribers.attribs->>'zip' LIKE '999%' "
        "OR subscribers.attribs->>'last_purchase' LIKE '%2026%'",
        attribs,
    )


def test_unknown_query_shape_is_not_guessed():
    with pytest.raises(QueryNotEvaluatable):
        evaluate_query("subscribers.email ILIKE '%@gmail.com'", {})


# --- reconciliation -----------------------------------------------------------


def test_full_fixture_reconciles(bundle, spec, migrated):
    report = reconcile(bundle, spec, migrated)
    assert report.suppression.total == 411
    assert report.suppression.pct == 100.0
    assert not report.suppression.violations
    assert all(c.ok for c in report.consent_counts)
    assert all(s.ok for s in report.segments)
    assert report.checksums.sampled >= 50
    assert not report.checksums.mismatches
    assert all(not c.residual_source_tags for c in report.campaigns)
    assert all(c.render_matches for c in report.campaigns)
    assert report.passed


def test_reconciliation_catches_a_resubscribed_suppression(bundle, spec, migrated):
    # Flip one blocklisted subscriber behind the executor's back.
    plan = build_plan(bundle, spec)
    victim = plan.suppressions[0].email.lower()
    migrated.subscribers[victim].status = "enabled"
    migrated.subscribers[victim].subscription = "confirmed"
    report = reconcile(bundle, spec, migrated)
    assert victim in [v.lower() for v in report.suppression.violations]
    assert report.suppression.pct < 100.0
    assert not report.passed


def test_reconciliation_catches_a_drifted_attribute(bundle, spec, migrated):
    plan = build_plan(bundle, spec)
    sample_email = sorted(d.email.lower() for d in plan.all_subscribers)[0]
    migrated.subscribers[sample_email].attribs["state"] = "ZZ"
    report = reconcile(bundle, spec, migrated)
    assert sample_email in [m.lower() for m in report.checksums.mismatches]
    assert not report.passed


def test_report_renders(bundle, spec, migrated):
    markdown = render_markdown(reconcile(bundle, spec, migrated))
    assert "Suppression integrity" in markdown
    assert "Segment parity" in markdown
    assert "**PASS**" in markdown


# --- cutover plan -------------------------------------------------------------


def test_cutover_plan_renders_required_sections(bundle, spec):
    plan = build_plan(bundle, spec)
    text = render_cutover_plan(plan, spec, ramp="Ramp by signup recency.")
    for heading in (
        "## Freeze window",
        "## Migration order",
        "## Parallel-run validation",
        "## Cutover checklist",
        "## Rollback",
        "## Sending ramp",
    ):
        assert heading in text
    assert "Suppressions first." in text
    assert "What cannot be rolled back." in text
    assert "411" in text  # counts trace to the plan
    assert "3,844" in text
    assert spec.approval.approved_by in text
