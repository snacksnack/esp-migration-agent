"""Risk register, proposal, and report rendering (RC1-544 acceptance)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from discovery.inventory import build_inventory
from discovery.narrator import narration_payload
from discovery.proposal import build_proposal
from discovery.report import build_report, render_markdown
from discovery.risks import RiskError, build_risk_register
from ingest.mailchimp import load_bundle
from seeding.generator import generate

AUDIENCE = Path("fixtures/mailchimp_export/audience")
SUPPLEMENT = Path("fixtures/api_supplement")
AS_OF = datetime(2026, 10, 24, tzinfo=UTC)


@pytest.fixture(scope="module")
def bundle():
    return load_bundle(AUDIENCE, SUPPLEMENT)


@pytest.fixture(scope="module")
def inventory(bundle):
    return build_inventory(bundle)


@pytest.fixture(scope="module")
def risks(bundle, inventory):
    return build_risk_register(bundle, inventory, target="listmonk")


@pytest.fixture(scope="module")
def expected_risk_counts():
    """Seeded data-quality risk counts, restricted to exportable members."""
    rejected = set(json.loads(Path("seeding/rejected_emails.json").read_text()))
    subs, _ = generate(count=4500, seed=542)
    kept = [s for s in subs if s.email not in rejected and s.status != "pending"]
    return Counter(r for s in kept for r in s.seeded_risks)


def test_seeded_data_quality_risks_detected(risks, expected_risk_counts):
    by_code = {r.code: r for r in risks}
    assert by_code["malformed_zip"].count == expected_risk_counts["bad_zip"]
    assert by_code["inconsistent_date_format"].count == expected_risk_counts["messy_date"]
    assert by_code["missing_first_name"].count == expected_risk_counts["missing_fname"]
    assert by_code["role_addresses"].count == expected_risk_counts["role_address"]


def test_unsupported_feature_risk_detected(risks):
    unsupported = [r for r in risks if r.code == "unsupported_segment"]
    assert len(unsupported) == 1
    assert "Highly engaged (4-5 stars)" in unsupported[0].title
    assert unsupported[0].severity == "high"


def test_platform_gap_and_degenerate_engagement_flagged(risks):
    codes = {r.code for r in risks}
    assert "pending_members_omitted" in codes
    assert "no_engagement_signal" in codes


def test_register_sorted_by_severity(risks):
    order = {"high": 0, "medium": 1, "low": 2}
    ranks = [order[r.severity] for r in risks]
    assert ranks == sorted(ranks)


def test_unknown_target_fails_loudly(bundle, inventory):
    with pytest.raises(RiskError, match="unknown target"):
        build_risk_register(bundle, inventory, target="sendgrid")


def test_proposal_drivers_trace_to_inventory(inventory, risks):
    proposal = build_proposal(inventory, risks)
    units = {w.driver: w.units for w in proposal.workstreams}
    assert units["suppression_count"] == inventory.suppression_count == 411
    assert units["members_total"] == inventory.members_total == 4255
    assert units["segments"] == len(inventory.segments) == 2
    assert units["templates"] == len(inventory.templates) == 2
    assert proposal.total_hours == round(sum(w.est_hours for w in proposal.workstreams), 1)
    assert any("Automations" in o for o in proposal.out_of_scope)


def test_report_renders_with_ground_truth_counts(bundle):
    report = build_report(bundle, as_of=AS_OF, target="listmonk")
    markdown = render_markdown(report)
    assert "4,255" in markdown  # members in export
    assert "| subscribed | 3,756 |" in markdown
    assert "| unsubscribed | 411 |" in markdown
    assert "Suppression list (migrates first): 411" in markdown.replace("*", "")
    assert "Audience export omits pending" in markdown
    assert "cannot be represented in listmonk" in markdown
    assert "## Scope and estimate proposal" in markdown
    assert "no usable engagement signal" in report.ramp_recommendation


def test_narration_payload_numbers_trace_to_profile(bundle, inventory, risks):
    report = build_report(bundle, as_of=AS_OF, target="listmonk")
    payload = narration_payload(report.profile, inventory, risks)
    assert payload["total_members_in_export"] == 4255
    assert payload["consent"]["subscribed"]["count"] == 3756
    assert payload["consent"]["subscribed"]["pct"] == round(100 * 3756 / 4255, 1)
    assert payload["member_rating_distribution"] == {"2": 4255}
    # No record-level PII: the payload is aggregates and object names only.
    assert "@" not in json.dumps(payload)
