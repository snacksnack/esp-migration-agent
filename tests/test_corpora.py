"""RC1-572: the synthetic breadth corpora - built deterministically, shaped
like the genuine capture, round-tripped through ingest against their ground
truth, and wired into discovery, the proposer payload, the eval case and the
plan builder. No network."""

from __future__ import annotations

import csv
import filecmp
import json
from pathlib import Path

import pytest

from discovery.inventory import build_inventory
from discovery.risks import build_risk_register
from executor.plan import PlanError, build_plan
from ingest.mailchimp import load_bundle
from mapping.gate import approve, load_approved
from mapping.proposer import ProposalError, assemble_spec, check_completeness, mapping_payload
from seeding.build_corpora import SYNTHETIC, build_all
from seeding.bundle_writer import write_audience_csvs
from seeding.generator import generate
from specs.model import MappingSpec

GENUINE_AUDIENCE = Path("fixtures/mailchimp_export/audience")
GENUINE_SUPPLEMENT = Path("fixtures/api_supplement")
APPROVED = Path("specs/approved/mailchimp_to_listmonk.yaml")
CUSTOM = SYNTHETIC / "custom_fields"
MULTI = SYNTHETIC / "multi_audience"
NEW_RISK_CODES = {
    "unmigrated_custom_fields", "multiple_audiences",
    "export_row_count_mismatch", "complex_merge_tags",
}


def truth(corpus: Path) -> dict:
    return json.loads((corpus / "ground_truth.json").read_text())


def risk_codes(audience: Path, supplement: Path) -> set[str]:
    bundle = load_bundle(audience, supplement)
    return {r.code for r in build_risk_register(bundle, build_inventory(bundle), "listmonk")}


def header(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return next(csv.reader(f))


# --- the corpora are built, not hand-edited ---------------------------------------


def test_committed_corpora_match_a_fresh_build(tmp_path):
    build_all(tmp_path)
    committed = sorted(p.relative_to(SYNTHETIC) for p in SYNTHETIC.rglob("*") if p.is_file())
    fresh = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    assert committed == fresh, "file set drifted; run python -m seeding.build_corpora"
    stale = [
        str(rel) for rel in committed
        if not filecmp.cmp(SYNTHETIC / rel, tmp_path / rel, shallow=False)
    ]
    assert not stale, f"edit seeding/build_corpora.py and rebuild, not the outputs: {stale}"


def test_writer_matches_the_genuine_capture_shape(tmp_path):
    """The synthetic writer's headers are the genuine export's headers, per
    bucket - including the UNSUB_* columns only the unsubscribed file has."""
    subs, _ = generate(60, seed=1)
    write_audience_csvs(subs, tmp_path, export_id="x")
    for bucket in ("subscribed", "unsubscribed", "nonsubscribed"):
        genuine = next(GENUINE_AUDIENCE.glob(f"{bucket}_*.csv"))
        assert header(tmp_path / f"{bucket}_email_audience_export_x.csv") == header(genuine)


def test_writer_reproduces_the_pending_export_gap(tmp_path):
    subs, gt = generate(450, seed=572)
    write_audience_csvs(subs, tmp_path)
    bundle = load_bundle(tmp_path, CUSTOM / "api_supplement")
    exported = {m.email for m in bundle.all_members}
    pending = {s.email for s in subs if s.status == "pending"}
    assert pending and not (exported & pending)
    assert len(exported) == gt.count - gt.consent["pending"]


def test_writer_timestamps_round_trip_through_the_account_timezone(tmp_path):
    subs, _ = generate(40, seed=3)
    write_audience_csvs(subs, tmp_path)
    by_email = {s.email: s for s in subs}
    bundle = load_bundle(tmp_path, CUSTOM / "api_supplement")
    for member in bundle.all_members:
        assert member.confirm_time == by_email[member.email].signup_at.replace(second=0)


# --- custom fields ------------------------------------------------------------------


def test_custom_fields_corpus_round_trips_against_ground_truth():
    gt = truth(CUSTOM)
    bundle = load_bundle(CUSTOM / "audience", CUSTOM / "api_supplement")
    assert {b.value: len(rows) for b, rows in bundle.members.items()} == gt["exported_by_bucket"]
    assert set(bundle.extra_columns) == {"LOYALTY_TIER", "POINTS"}
    # Every distinct tier survives into the samples, padding included.
    assert bundle.extra_columns["LOYALTY_TIER"] == gt["custom_fields"]["LOYALTY_TIER"]
    assert "GOLD " in bundle.extra_columns["LOYALTY_TIER"]


def test_genuine_fixture_has_no_custom_columns():
    # Mailchimp's standard-but-empty fields (Address, Birthday, ...) must not
    # surface as custom data: only data-bearing unknown columns do.
    assert load_bundle(GENUINE_AUDIENCE, GENUINE_SUPPLEMENT).extra_columns == {}


def test_custom_fields_corpus_raises_its_seeded_risks():
    codes = risk_codes(CUSTOM / "audience", CUSTOM / "api_supplement")
    assert set(truth(CUSTOM)["expected_risk_codes"]) <= codes
    assert "export_row_count_mismatch" not in codes  # the export is complete


def test_genuine_fixture_raises_none_of_the_breadth_risks():
    assert not (risk_codes(GENUINE_AUDIENCE, GENUINE_SUPPLEMENT) & NEW_RISK_CODES)


def test_custom_fields_reach_the_proposer_payload():
    bundle = load_bundle(CUSTOM / "audience", CUSTOM / "api_supplement")
    payload = mapping_payload(bundle, build_inventory(bundle), "listmonk")
    by_field = {f["source_field"]: f for f in payload["fields"]}
    assert by_field["LOYALTY_TIER"]["sample_values"] == bundle.extra_columns["LOYALTY_TIER"]
    assert "IF:FNAME" in payload["merge_tags_in_use"]


def test_frozen_eval_payload_matches_the_corpus():
    """The eval case is frozen, but frozen FROM the corpus: if either drifts,
    re-freeze rather than scoring a payload nothing produces any more."""
    bundle = load_bundle(CUSTOM / "audience", CUSTOM / "api_supplement")
    live = mapping_payload(bundle, build_inventory(bundle), "listmonk")
    frozen = json.loads(Path("evals/cases/custom_fields_listmonk.json").read_text())
    assert live == frozen


def _draft_covering(payload: dict, *, skip_field: str | None = None) -> dict:
    return {
        "field_mappings": [
            {"source_field": f["source_field"], "source_type": f["source_type"],
             "sample_values": [], "target_field": None, "transform": "Pending.",
             "confidence": 0.3, "rationale": "r", "status": "flagged",
             "open_question": "q"}
            for f in payload["fields"] if f["source_field"] != skip_field
        ],
        "merge_tag_mappings": [
            {"source_tag": t, "target_tag": "{{ x }}"} for t in payload["merge_tags_in_use"]
        ],
        "segment_mappings": [
            {"source_segment": s["name"], "source_logic": "l", "target_query": None,
             "confidence": 0.3, "rationale": "r", "status": "flagged", "open_question": "q"}
            for s in payload["segments"]
        ],
        "consent_mappings": [
            {"source_status": b, "target_status": None, "confidence": 0.3,
             "rationale": "r", "status": "flagged", "open_question": "q"}
            for b in payload["consent_buckets"]["observed"]
        ],
    }


def test_a_proposal_that_drops_a_custom_field_is_refused():
    bundle = load_bundle(CUSTOM / "audience", CUSTOM / "api_supplement")
    payload = mapping_payload(bundle, build_inventory(bundle), "listmonk")
    check_completeness(assemble_spec(_draft_covering(payload), "mailchimp", "listmonk"), payload)
    dropped = assemble_spec(
        _draft_covering(payload, skip_field="LOYALTY_TIER"), "mailchimp", "listmonk"
    )
    with pytest.raises(ProposalError, match="LOYALTY_TIER"):
        check_completeness(dropped, payload)


# --- the plan refuses what the executor cannot migrate --------------------------------


def _approved_variant(edit) -> MappingSpec:
    data = load_approved(APPROVED).model_dump(mode="json")
    data["approval"] = None
    data["status"] = "draft"
    edit(data)
    return approve(MappingSpec.model_validate(data), approved_by="test reviewer")


def test_plan_refuses_untranslated_conditional_merge_tags():
    bundle = load_bundle(CUSTOM / "audience", CUSTOM / "api_supplement")
    with pytest.raises(PlanError, match="IF:FNAME"):
        build_plan(bundle, load_approved(APPROVED))


def test_plan_refuses_a_spec_that_maps_a_custom_field():
    """A reviewer may approve a mapping for LOYALTY_TIER, but the executor's
    fixed field set cannot read it: refused loudly, never dropped."""

    def add_custom_mapping(data: dict) -> None:
        template = next(m for m in data["field_mappings"] if m["source_field"] == "STATE")
        data["field_mappings"].append(
            {**template, "source_field": "LOYALTY_TIER",
             "target_field": "attribs.loyalty_tier", "status": "edited"}
        )

    bundle = load_bundle(CUSTOM / "audience", CUSTOM / "api_supplement")
    bundle.campaigns = []  # isolate the field refusal from the merge-tag one
    with pytest.raises(PlanError, match="unknown source field 'LOYALTY_TIER'"):
        build_plan(bundle, _approved_variant(add_custom_mapping))
