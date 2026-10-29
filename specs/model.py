"""Mapping-spec schema: the stage-4 reviewable document (ADR-0003).

Structural invariants live here, not in review discipline: a flagged mapping
must ask a question instead of guessing a target, an approved spec cannot
contain unresolved mappings, and the suppression ordering invariant is
unrepresentable any other way.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


def hash_content(content: dict) -> str:
    """Canonical hash of a spec's reviewed content; shared by content_sha256
    and the review gate so both sides pin exactly the same bytes."""
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


class MappingStatus(StrEnum):
    PROPOSED = "proposed"  # LLM output, untouched
    APPROVED = "approved"  # reviewer accepted as proposed
    EDITED = "edited"  # reviewer changed it; the edit is the mapping
    FLAGGED = "flagged"  # ambiguous: carries an open question, never a guess


class SpecStatus(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"


class ReviewedMapping(BaseModel):
    """Common shape of every LLM-proposed mapping: confidence, rationale, review status."""

    model_config = ConfigDict(extra="forbid")

    # Name of the subclass field that may be omitted only while flagged.
    target_attr: ClassVar[str | None] = None

    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1)
    status: MappingStatus = MappingStatus.PROPOSED
    open_question: str | None = None

    @model_validator(mode="after")
    def _flag_dont_guess(self) -> ReviewedMapping:
        if self.status is MappingStatus.FLAGGED:
            if not self.open_question:
                raise ValueError("a flagged mapping must carry an open_question")
        elif self.target_attr and not getattr(self, self.target_attr):
            raise ValueError(
                f"{self.target_attr} is required unless the mapping is flagged"
            )
        return self


class FieldMapping(ReviewedMapping):
    target_attr: ClassVar[str] = "target_field"

    source_field: str
    source_type: str
    sample_values: list[str] = []
    target_field: str | None = None
    transform: str = "none"


class SegmentMapping(ReviewedMapping):
    target_attr: ClassVar[str] = "target_query"

    source_segment: str
    source_logic: str
    target_query: str | None = None


class ConsentMapping(ReviewedMapping):
    target_attr: ClassVar[str] = "target_status"

    source_status: str
    target_status: str | None = None


class MergeTagMapping(BaseModel):
    """Mechanical tag translation (*|FNAME|* -> template syntax); no judgment, no status."""

    model_config = ConfigDict(extra="forbid")

    source_tag: str
    target_tag: str


class SuppressionPolicy(BaseModel):
    """The hard rule of RC1-542, held structurally: suppressions migrate first,
    and the schema cannot express any other ordering."""

    model_config = ConfigDict(extra="forbid")

    order: Literal["before_subscribers"] = "before_subscribers"
    source_statuses: list[str] = ["unsubscribed", "cleaned"]


class ApproverIdentity(BaseModel):
    """Who signed off, as established by the sign-in rather than typed into a
    box (RC1-554). verified=True only for an IdP-federated login; the demo's
    shared password records method="password" and verified=False, so a report
    never presents a typed name as an authenticated one."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["oidc", "password"]
    verified: bool
    issuer: str | None = None  # the IdP's OIDC issuer URL
    subject: str | None = None  # the IdP's stable user id (sub claim)
    email: str | None = None
    name: str | None = None

    def describe(self) -> str:
        if self.method == "oidc":
            who = self.email or self.subject
            return f"verified via OIDC ({self.issuer}, sub {self.subject}, {who})"
        return "unverified: shared demo password, name typed by the approver"


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved_by: str = Field(min_length=1)
    approved_at: datetime
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # RC1-554. Outside the content hash (the approval block always is), and
    # omitted from YAML when unset, so earlier approvals stay byte-identical.
    identity: ApproverIdentity | None = None

    def identity_line(self) -> str:
        if self.identity is None:
            return "identity not recorded (CLI or pre-RC1-554 approval; name is free text)"
        return self.identity.describe()


# Optional provenance fields the approval binds when present; unset fields
# are omitted from hashing and YAML so older signed specs stay valid.
PROVENANCE_FIELDS = ("source_fingerprint", "source_timezone", "source_audience")


class MappingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec_version: Literal[1] = 1
    source: str
    target: str
    # Source provenance the approval binds (RC1-566/RC1-569): sha256 of the
    # export files the proposal was built from, and the account timezone the
    # export was parsed with (timestamps are tz-localized before converting
    # to UTC, so a different tz yields different target values from the same
    # files). The approval's content hash covers both, so a signed spec only
    # runs against the exact inputs that were reviewed. None on older specs.
    source_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_timezone: str | None = Field(default=None, min_length=1)
    # Which audience the export is (RC1-573): names the destination list and
    # scopes the campaigns, so two audiences of one account land in two
    # lists. Bound by the approval like the fields above. None on older specs
    # (the legacy single list name; refused if the supplement spans several).
    source_audience: str | None = Field(default=None, min_length=1)
    generated_at: datetime
    status: SpecStatus = SpecStatus.DRAFT
    field_mappings: list[FieldMapping] = []
    merge_tag_mappings: list[MergeTagMapping] = []
    segment_mappings: list[SegmentMapping] = []
    consent_mappings: list[ConsentMapping] = []
    suppressions: SuppressionPolicy = SuppressionPolicy()
    approval: Approval | None = None

    @model_validator(mode="after")
    def _approved_means_resolved(self) -> MappingSpec:
        if self.status is not SpecStatus.APPROVED:
            return self
        if self.approval is None:
            raise ValueError("an approved spec must carry an approval block")
        unresolved = [
            m for m in self.reviewed_mappings()
            if m.status in (MappingStatus.PROPOSED, MappingStatus.FLAGGED)
        ]
        if unresolved:
            raise ValueError(
                f"an approved spec may not contain {len(unresolved)} unresolved "
                "(proposed or flagged) mappings"
            )
        return self

    def reviewed_mappings(self) -> list[ReviewedMapping]:
        return [*self.field_mappings, *self.segment_mappings, *self.consent_mappings]

    def open_questions(self) -> list[str]:
        return [m.open_question for m in self.reviewed_mappings() if m.open_question]

    def _unset_provenance(self) -> set[str]:
        """Provenance fields that are unset are absent, not null, from hashing
        and YAML, so specs written before each field existed keep their
        content hash (RC1-566/RC1-569)."""
        return {f for f in PROVENANCE_FIELDS if getattr(self, f) is None}

    def content_sha256(self) -> str:
        """Hash of the reviewed content (everything except the approval block),
        pinned into Approval so the executor runs exactly what was signed off."""
        exclude = {"approval"} | self._unset_provenance()
        return hash_content(self.model_dump(mode="json", exclude=exclude))

    @classmethod
    def from_yaml(cls, text: str) -> MappingSpec:
        return cls.model_validate(yaml.safe_load(text))

    def to_yaml(self) -> str:
        exclude: dict = dict.fromkeys(self._unset_provenance(), True)
        if self.approval is not None and self.approval.identity is None:
            exclude["approval"] = {"identity": True}
        return yaml.safe_dump(
            self.model_dump(mode="json", exclude=exclude or None),
            sort_keys=False,
        )
