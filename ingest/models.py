"""Typed inventory of a parsed Mailchimp export bundle (RC1-544).

These models are what every downstream stage reads: the discovery profile,
the risk register, and (in Phase 2) the mapping proposer. Parsers live in
ingest/mailchimp.py; nothing here does I/O.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class ConsentBucket(StrEnum):
    """The three files a Mailchimp audience export splits members into.

    Named as the source platform names them. "pending" members appear in NO
    export file - a documented platform gap the risk register reports.
    """

    SUBSCRIBED = "subscribed"
    UNSUBSCRIBED = "unsubscribed"
    NONSUBSCRIBED = "nonsubscribed"


class SubscriberRecord(BaseModel):
    email: str
    bucket: ConsentBucket
    fname: str
    lname: str
    zip_code: str
    state: str
    last_purchase: str
    member_rating: int
    optin_time: datetime | None
    confirm_time: datetime | None  # Mailchimp stores the seeded signup timestamp here
    unsub_time: datetime | None = None


class SegmentCondition(BaseModel):
    condition_type: str
    field: str
    op: str
    value: str | int | float


class SegmentDef(BaseModel):
    id: int
    name: str
    type: str
    match: str
    conditions: list[SegmentCondition]
    member_count: int


class TemplateMeta(BaseModel):
    id: int
    name: str
    type: str


class CampaignMeta(BaseModel):
    id: str
    status: str
    type: str
    title: str
    subject_line: str
    from_name: str
    list_id: str
    # The audience's NAME: the identity an audience export and the account
    # export share (account files are <bucket>_<id>_<name>.csv; the audience
    # CSVs carry none). Empty when the supplement predates RC1-573.
    list_name: str = ""
    recipient_count: int
    saved_segment_id: int | None
    html: str
    merge_tags: list[str]


class MailchimpBundle(BaseModel):
    """Everything parsed from one source account: audience export CSVs plus
    the API supplement (segment definitions, templates, draft campaigns)."""

    members: dict[ConsentBucket, list[SubscriberRecord]]
    segments: list[SegmentDef]
    templates: list[TemplateMeta]
    campaigns: list[CampaignMeta]
    # Custom merge-field columns outside the standard export set that CARRY
    # DATA (RC1-572): column -> a few distinct sample values. The parser does
    # not migrate them - they surface here so discovery risks them and the
    # proposer must flag or map them, never silently drop them.
    extra_columns: dict[str, list[str]] = {}

    @property
    def all_members(self) -> list[SubscriberRecord]:
        return [m for bucket in ConsentBucket for m in self.members.get(bucket, [])]

    @property
    def suppressed(self) -> list[SubscriberRecord]:
        """The suppression list: who must migrate FIRST (the hard invariant)."""
        return self.members.get(ConsentBucket.UNSUBSCRIBED, [])
