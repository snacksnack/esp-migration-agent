from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from specs.model import (
    Approval,
    FieldMapping,
    MappingSpec,
    MappingStatus,
    SpecStatus,
    SuppressionPolicy,
)

EXAMPLE = Path(__file__).parent.parent / "specs/examples/mailchimp_to_listmonk.yaml"


def example_spec() -> MappingSpec:
    return MappingSpec.from_yaml(EXAMPLE.read_text())


def approved_field(**overrides) -> FieldMapping:
    base = dict(
        source_field="FNAME",
        source_type="text",
        target_field="attribs.first_name",
        confidence=0.9,
        rationale="standard first-name field",
        status=MappingStatus.APPROVED,
    )
    return FieldMapping(**{**base, **overrides})


def test_worked_example_validates():
    spec = example_spec()
    assert spec.source == "mailchimp" and spec.target == "listmonk"
    assert spec.status is SpecStatus.DRAFT
    flagged = [m for m in spec.reviewed_mappings() if m.status is MappingStatus.FLAGGED]
    assert len(flagged) == 2
    assert len(spec.open_questions()) == 2


def test_flagged_mapping_requires_open_question():
    with pytest.raises(ValidationError, match="open_question"):
        approved_field(status=MappingStatus.FLAGGED, target_field=None, open_question=None)


def test_unflagged_mapping_requires_target():
    with pytest.raises(ValidationError, match="target_field is required"):
        approved_field(target_field=None)


def test_flagged_mapping_may_omit_target():
    m = approved_field(
        status=MappingStatus.FLAGGED, target_field=None, open_question="which format?"
    )
    assert m.target_field is None


def test_suppression_order_is_unrepresentable_any_other_way():
    with pytest.raises(ValidationError):
        SuppressionPolicy(order="after_subscribers")


def test_approved_spec_requires_approval_block():
    with pytest.raises(ValidationError, match="approval block"):
        MappingSpec(
            source="mailchimp",
            target="listmonk",
            generated_at=datetime.now(UTC),
            status=SpecStatus.APPROVED,
            field_mappings=[approved_field()],
        )


def test_approved_spec_rejects_unresolved_mappings():
    draft = example_spec()  # contains proposed + flagged mappings
    data = draft.model_dump(mode="json")
    data["status"] = "approved"
    data["approval"] = {
        "approved_by": "customer@example.com",
        "approved_at": "2026-10-24T12:00:00Z",
        "content_sha256": "0" * 64,
    }
    with pytest.raises(ValidationError, match="unresolved"):
        MappingSpec.model_validate(data)


def test_fully_resolved_spec_approves():
    spec = MappingSpec(
        source="mailchimp",
        target="listmonk",
        generated_at=datetime.now(UTC),
        field_mappings=[approved_field()],
    )
    approved = spec.model_copy(
        update={
            "status": SpecStatus.APPROVED,
            "approval": Approval(
                approved_by="customer@example.com",
                approved_at=datetime.now(UTC),
                content_sha256=spec.content_sha256(),
            ),
        }
    )
    assert MappingSpec.model_validate(approved.model_dump(mode="json"))


def test_content_hash_excludes_approval_and_is_stable():
    spec = example_spec()
    h1 = spec.content_sha256()
    with_approval = spec.model_copy(
        update={
            "approval": Approval(
                approved_by="x",
                approved_at=datetime.now(UTC),
                content_sha256=h1,
            )
        }
    )
    assert with_approval.content_sha256() == h1


def test_yaml_round_trip():
    spec = example_spec()
    assert MappingSpec.from_yaml(spec.to_yaml()) == spec
