"""The free layer of the RC1-548 suite: the scorer itself, offline.

Builds model outputs programmatically from the frozen case payloads - a
compliant draft first, then single-defect perturbations - and asserts the
scorer catches exactly the defect and nothing else. No network, no key.
"""

from __future__ import annotations

import pytest

from evals import expectations, subject
from mapping.proposer import assemble_spec

TARGETS = {
    "EMAIL": ("email", "Copy as-is."),
    "FNAME": ("attribs.first_name", "Copy into attribs."),
    "LNAME": ("attribs.last_name", "Copy into attribs."),
    "STATE": ("attribs.state", "Copy as-is."),
    "OPTIN_TIME": ("attribs.optin_time", "ISO 8601 UTC string."),
    "CONFIRM_TIME": ("attribs.confirm_time", "ISO 8601 UTC string."),
    "ZIP": ("attribs.zip", "Copy as-is (uniform 5-digit)."),
    "LASTPURCH": ("attribs.lastpurch", "Copy as-is (uniform ISO)."),
    "POINTS": ("attribs.points", "Parse as integer."),
}


def compliant_output(case_id: str) -> dict:
    """A draft that follows the contract for the given case: flag exactly the
    seeded ambiguities plus the judgment calls, propose the rest."""
    payload = subject.load_payload(next(c for c in subject.CASES if c.id == case_id))
    exp = expectations.BY_CASE[case_id]
    judgment_flag_all = True  # judgment calls may land either way; flag them here

    def disposition(kind: str, name: str) -> dict:
        key = (kind, name)
        if key in exp.must_propose:
            return {"status": "proposed", "open_question": None}
        if key in exp.must_flag or judgment_flag_all:
            return {"status": "flagged", "open_question": f"reviewer decision on {name}"}
        return {"status": "proposed", "open_question": None}

    fields = []
    for f in payload["fields"]:
        d = disposition("field", f["source_field"])
        target, transform = (
            TARGETS[f["source_field"]] if d["status"] == "proposed" else (None, "Pending.")
        )
        fields.append(
            {
                "source_field": f["source_field"],
                "source_type": f["source_type"],
                "sample_values": [],
                "target_field": target,
                "transform": transform,
                "confidence": 0.9 if d["status"] == "proposed" else 0.3,
                "rationale": "test fixture rationale",
                **d,
            }
        )
    segments = []
    for s in payload["segments"]:
        d = disposition("segment", s["name"])
        proposed_query = "subscribers.attribs->>'x' = 'y'"
        segments.append(
            {
                "source_segment": s["name"],
                "source_logic": "restated logic",
                "target_query": proposed_query if d["status"] == "proposed" else None,
                "confidence": 0.5,
                "rationale": "test fixture rationale",
                **d,
            }
        )
    consents = []
    for c in payload["consent_buckets"]["observed"]:
        d = disposition("consent", c)
        consents.append(
            {
                "source_status": c,
                "target_status": (
                    "subscriber status: enabled; list subscription: confirmed"
                    if d["status"] == "proposed"
                    else None
                ),
                "confidence": 0.8,
                "rationale": "test fixture rationale",
                **d,
            }
        )
    return {
        "field_mappings": fields,
        "merge_tag_mappings": [],
        "segment_mappings": segments,
        "consent_mappings": consents,
    }


def scored(case_id: str, output: dict):
    payload = subject.load_payload(next(c for c in subject.CASES if c.id == case_id))
    spec = assemble_spec(output, source="mailchimp", target="listmonk")
    return subject.score(spec, payload, expectations.BY_CASE[case_id])


def by_name(results):
    return {r.name: r for r in results}


def test_compliant_fixture_draft_passes_everything():
    results, obs = scored("fixture-ambiguities", compliant_output("fixture-ambiguities"))
    gated = [r for r in results if not r.advisory]
    assert all(r.passed for r in gated), [r.detail for r in gated if not r.passed]
    assert obs["mapping_accuracy"] == 1.0
    assert obs["flag_recall"] == 1.0
    assert obs["flag_precision"] == 1.0


def test_compliant_clean_draft_passes_everything():
    results, obs = scored("clean-variant", compliant_output("clean-variant"))
    gated = [r for r in results if not r.advisory]
    assert all(r.passed for r in gated), [r.detail for r in gated if not r.passed]
    assert obs["flag_recall"] is None  # nothing seeded: a rate of zero would lie


def test_guessing_a_seeded_ambiguity_fails_recall():
    output = compliant_output("fixture-ambiguities")
    zip_mapping = next(f for f in output["field_mappings"] if f["source_field"] == "ZIP")
    zip_mapping.update(
        status="proposed", open_question=None, target_field="attribs.zip", transform="Pad."
    )
    results, obs = scored("fixture-ambiguities", output)
    named = by_name(results)
    assert not named["seeded-ambiguities-flagged"].passed
    assert "ZIP" in named["seeded-ambiguities-flagged"].detail
    assert obs["flag_recall"] == pytest.approx(2 / 3)


def test_flagging_an_unambiguous_field_fails_precision():
    output = compliant_output("clean-variant")
    email = next(f for f in output["field_mappings"] if f["source_field"] == "EMAIL")
    email.update(status="flagged", open_question="which column?", target_field=None)
    results, obs = scored("clean-variant", output)
    named = by_name(results)
    assert not named["unambiguous-not-flagged"].passed
    assert "EMAIL" in named["unambiguous-not-flagged"].detail
    assert obs["flag_precision"] < 1.0


def test_wrong_target_concept_fails_accuracy():
    output = compliant_output("fixture-ambiguities")
    fname = next(f for f in output["field_mappings"] if f["source_field"] == "FNAME")
    fname["target_field"] = "attribs.surname"
    results, obs = scored("fixture-ambiguities", output)
    named = by_name(results)
    assert not named["unambiguous-targets-correct"].passed
    assert obs["mapping_accuracy"] == pytest.approx(6 / 7)


def test_flag_with_a_smuggled_resolution_is_caught():
    output = compliant_output("fixture-ambiguities")
    lastpurch = next(f for f in output["field_mappings"] if f["source_field"] == "LASTPURCH")
    lastpurch["target_field"] = "attribs.lastpurch"  # still flagged, but resolved
    results, _ = scored("fixture-ambiguities", output)
    assert not by_name(results)["flagged-mappings-omit-resolution"].passed


def test_dropping_a_consent_bucket_fails_coverage():
    output = compliant_output("fixture-ambiguities")
    output["consent_mappings"] = [
        c for c in output["consent_mappings"] if c["source_status"] != "nonsubscribed"
    ]
    results, _ = scored("fixture-ambiguities", output)
    named = by_name(results)
    assert not named["exhaustive-coverage"].passed
    assert "nonsubscribed" in named["exhaustive-coverage"].detail


def test_judgment_calls_are_advisory_only():
    results, _ = scored("fixture-ambiguities", compliant_output("fixture-ambiguities"))
    named = by_name(results)
    assert named["judgment-calls"].advisory
    assert "MEMBER_RATING" in named["judgment-calls"].detail


def test_cases_have_expectations():
    assert {c.id for c in subject.CASES} == set(expectations.BY_CASE)


# --- RC1-572: the custom-fields breadth case -------------------------------------


def test_compliant_custom_fields_draft_passes_everything():
    results, obs = scored("custom-fields", compliant_output("custom-fields"))
    gated = [r for r in results if not r.advisory]
    assert all(r.passed for r in gated), [r.detail for r in gated if not r.passed]
    assert obs["flag_recall"] == 1.0
    assert obs["flag_precision"] == 1.0


def test_normalizing_a_mixed_custom_field_fails_recall():
    """Proposing a lowercase-and-trim rule for LOYALTY_TIER decides that
    'gold', 'GOLD ' and '2' are tiers the customer never said they were."""
    output = compliant_output("custom-fields")
    tier = next(f for f in output["field_mappings"] if f["source_field"] == "LOYALTY_TIER")
    tier.update(
        status="proposed", open_question=None,
        target_field="attribs.loyalty_tier", transform="Lowercase and trim.",
    )
    results, obs = scored("custom-fields", output)
    named = by_name(results)
    assert not named["seeded-ambiguities-flagged"].passed
    assert "LOYALTY_TIER" in named["seeded-ambiguities-flagged"].detail
    assert obs["flag_recall"] == pytest.approx(2 / 3)


def test_flagging_the_uniform_custom_field_fails_precision():
    output = compliant_output("custom-fields")
    points = next(f for f in output["field_mappings"] if f["source_field"] == "POINTS")
    points.update(status="flagged", open_question="what are points?", target_field=None)
    results, _ = scored("custom-fields", output)
    named = by_name(results)
    assert not named["unambiguous-not-flagged"].passed
    assert "POINTS" in named["unambiguous-not-flagged"].detail
