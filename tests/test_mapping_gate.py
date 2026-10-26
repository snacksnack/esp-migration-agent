"""RC1-545 acceptance: the proposer's deterministic boundaries and the review
gate. The LLM network path is never called; proposer tests exercise the
payload and the assembly/validation layer with canned model output."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from discovery.inventory import build_inventory
from discovery.risks import Risk, Severity, sort_risks
from ingest.mailchimp import load_bundle
from mapping.gate import GateError, approve, load_approved, load_spec
from mapping.proposer import ProposalError, assemble_spec, mapping_payload
from mapping.risks import spec_risks
from specs.model import MappingSpec, SpecStatus

AUDIENCE = Path("fixtures/mailchimp_export/audience")
SUPPLEMENT = Path("fixtures/api_supplement")
EXAMPLE = Path(__file__).parent.parent / "specs/examples/mailchimp_to_listmonk.yaml"


@pytest.fixture(scope="module")
def bundle():
    return load_bundle(AUDIENCE, SUPPLEMENT)


@pytest.fixture(scope="module")
def payload(bundle):
    return mapping_payload(bundle, build_inventory(bundle), target="listmonk")


def example_spec() -> MappingSpec:
    return MappingSpec.from_yaml(EXAMPLE.read_text())


def resolve_all(spec: MappingSpec) -> MappingSpec:
    """Simulate the reviewer's YAML edit: accept proposals, resolve flags."""
    data = spec.model_dump(mode="json")
    for kind, target_attr in (
        ("field_mappings", "target_field"),
        ("segment_mappings", "target_query"),
        ("consent_mappings", "target_status"),
    ):
        for m in data[kind]:
            if m["status"] == "flagged":
                m["status"] = "edited"
                m[target_attr] = m[target_attr] or "reviewer_decided_target"
            elif m["status"] == "proposed":
                m["status"] = "approved"
    return MappingSpec.model_validate(data)


# --- proposer payload: deterministic and complete ---------------------------


def test_payload_is_deterministic(bundle):
    inventory = build_inventory(bundle)
    assert mapping_payload(bundle, inventory, "listmonk") == mapping_payload(
        bundle, inventory, "listmonk"
    )


def test_payload_carries_the_seeded_ambiguities(payload):
    segment_names = {s["name"] for s in payload["segments"]}
    assert {"VIP customers", "VIP (legacy)"} <= segment_names  # overlapping VIPs
    lastpurch = next(f for f in payload["fields"] if f["source_field"] == "LASTPURCH")
    assert len(lastpurch["observed_formats"]) > 1  # mixed date formats
    assert "nonsubscribed" in payload["consent_buckets"]["observed"]  # unclear consent


def test_payload_segments_include_definitions(payload):
    assert all(s["conditions"] for s in payload["segments"])


def test_payload_rejects_unknown_target(bundle):
    with pytest.raises(ProposalError, match="unknown target"):
        mapping_payload(bundle, build_inventory(bundle), "sendgrid")


# --- proposer output assembly: Python owns the shell ------------------------

CANNED_OUTPUT = {
    "field_mappings": [
        {
            "source_field": "EMAIL",
            "source_type": "email",
            "target_field": "email",
            "transform": "lowercase",
            "confidence": 1.0,
            "rationale": "primary key on both sides",
            "status": "proposed",
        },
        {
            "source_field": "LASTPURCH",
            "source_type": "text",
            "transform": "none",
            "confidence": 0.4,
            "rationale": "mixed formats",
            "status": "flagged",
            "open_question": "parse with which date rule, or migrate as text?",
        },
    ],
    "merge_tag_mappings": [
        {"source_tag": "*|FNAME|*", "target_tag": "{{ .Subscriber.Attribs.first_name }}"}
    ],
    "segment_mappings": [],
    "consent_mappings": [],
}


def test_assemble_spec_builds_a_draft():
    spec = assemble_spec(CANNED_OUTPUT, source="mailchimp", target="listmonk")
    assert spec.status is SpecStatus.DRAFT
    assert spec.approval is None
    assert spec.suppressions.order == "before_subscribers"
    assert spec.open_questions() == ["parse with which date rule, or migrate as text?"]


def test_assemble_spec_rejects_pre_resolved_mappings():
    output = {
        **CANNED_OUTPUT,
        "field_mappings": [{**CANNED_OUTPUT["field_mappings"][0], "status": "approved"}],
    }
    with pytest.raises(ProposalError, match="pre-resolved"):
        assemble_spec(output, source="mailchimp", target="listmonk")


def test_assemble_spec_rejects_a_guess():
    # Flagged but silently guessing is the defect the schema exists to catch:
    # flagged with no open_question fails validation.
    guess = {
        **CANNED_OUTPUT,
        "field_mappings": [
            {
                **CANNED_OUTPUT["field_mappings"][1],
                "open_question": None,
            }
        ],
    }
    with pytest.raises(ProposalError, match="validation"):
        assemble_spec(guess, source="mailchimp", target="listmonk")


def test_assemble_spec_rejects_empty_output():
    with pytest.raises(ProposalError, match="no mappings"):
        assemble_spec(
            {"field_mappings": [], "merge_tag_mappings": []},
            source="mailchimp",
            target="listmonk",
        )


# --- review gate: nothing downstream reads an unapproved spec ---------------


def test_approve_refuses_unresolved_mappings():
    with pytest.raises(GateError, match="unresolved"):
        approve(example_spec(), approved_by="Reid Collins")


def test_approve_stamps_identity_and_matching_hash():
    approved = approve(
        resolve_all(example_spec()),
        approved_by="Reid Collins",
        now=datetime(2026, 10, 26, 12, 0, tzinfo=UTC),
    )
    assert approved.status is SpecStatus.APPROVED
    assert approved.approval.approved_by == "Reid Collins"
    assert approved.content_sha256() == approved.approval.content_sha256


def test_load_approved_refuses_a_draft(tmp_path):
    path = tmp_path / "draft.yaml"
    path.write_text(example_spec().to_yaml())
    with pytest.raises(GateError, match="not approved"):
        load_approved(path)


def test_load_approved_refuses_post_approval_tampering(tmp_path):
    approved = approve(resolve_all(example_spec()), approved_by="Reid Collins")
    tampered = approved.model_dump(mode="json")
    tampered["field_mappings"][0]["target_field"] = "attribs.sneaky"
    path = tmp_path / "tampered.yaml"
    path.write_text(MappingSpec.model_validate(tampered).to_yaml())
    with pytest.raises(GateError, match="hash mismatch"):
        load_approved(path)


def test_load_approved_round_trip(tmp_path):
    approved = approve(resolve_all(example_spec()), approved_by="Reid Collins")
    path = tmp_path / "approved.yaml"
    path.write_text(approved.to_yaml())
    assert load_approved(path) == approved


def test_load_spec_is_loud_on_garbage(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("spec_version: 1\nsource: mailchimp\n")
    with pytest.raises(GateError, match="does not validate"):
        load_spec(path)
    with pytest.raises(GateError, match="no spec at"):
        load_spec(tmp_path / "missing.yaml")


# --- risk-register tie-in ----------------------------------------------------


def test_flagged_mappings_become_register_entries():
    risks = spec_risks(example_spec())  # the example carries 2 flagged mappings
    assert len(risks) == 2
    assert all(r.code == "mapping_open_question" for r in risks)
    assert all(r.severity is Severity.MEDIUM for r in risks)
    assert all(r.detail for r in risks)  # the open question itself


def test_approved_spec_contributes_no_risks():
    assert spec_risks(resolve_all(example_spec())) == []


def test_merged_register_stays_severity_ranked():
    merged = sort_risks(
        [
            Risk(code="x", severity=Severity.LOW, title="low", detail="d"),
            *spec_risks(example_spec()),
            Risk(code="y", severity=Severity.HIGH, title="high", detail="d"),
        ]
    )
    assert [r.severity for r in merged] == [
        Severity.HIGH,
        Severity.MEDIUM,
        Severity.MEDIUM,
        Severity.LOW,
    ]
