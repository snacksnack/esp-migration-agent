"""Deterministic inventory of the source account (RC1-544).

What the customer has: members per consent bucket, merge fields with their
OBSERVED value formats (not the declared types - observed formats are what
break migrations), segments with condition types, templates, campaigns, and
the suppression count. The proposal's effort drivers trace to these counts.
"""

from __future__ import annotations

import re
from collections import Counter

from pydantic import BaseModel

from ingest.models import MailchimpBundle

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OTHER_DATE_RE = re.compile(r"^(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|[A-Za-z]{3} \d{1,2} \d{4})$")

# Role accounts: list-bought/shared inboxes that bounce and complain at
# disproportionate rates - a standing deliverability check in ESP practice.
ROLE_LOCALPARTS = frozenset(
    {"info", "postmaster", "admin", "sales", "support", "abuse", "webmaster", "noreply"}
)


def classify_zip(value: str) -> str:
    if not value:
        return "empty"
    if re.fullmatch(r"\d{5}", value):
        return "zip_5_digit"
    if re.fullmatch(r"\d{3,4}", value):
        return "zip_truncated"  # classic lost-leading-zero artifact
    return "non_numeric"


def classify_purchase_date(value: str) -> str:
    if not value:
        return "empty"
    if ISO_DATE_RE.fullmatch(value):
        return "iso_date"
    if OTHER_DATE_RE.fullmatch(value):
        return "other_date_format"
    return "free_text"


def is_role_address(email: str) -> bool:
    return email.split("@", 1)[0] in ROLE_LOCALPARTS


class SegmentSummary(BaseModel):
    name: str
    match: str
    condition_types: list[str]
    member_count: int


class Inventory(BaseModel):
    lists: int
    members_total: int
    members_by_bucket: dict[str, int]
    suppression_count: int
    merge_field_formats: dict[str, dict[str, int]]
    missing_fname: int
    role_addresses: int
    segments: list[SegmentSummary]
    templates: list[str]
    campaigns_by_status: dict[str, int]
    merge_tags_in_use: list[str]


def build_inventory(bundle: MailchimpBundle) -> Inventory:
    members = bundle.all_members
    zip_formats = Counter(classify_zip(m.zip_code) for m in members)
    date_formats = Counter(classify_purchase_date(m.last_purchase) for m in members)
    merge_tags = {tag for c in bundle.campaigns for tag in c.merge_tags}
    return Inventory(
        lists=len({c.list_id for c in bundle.campaigns}) or 1,
        members_total=len(members),
        members_by_bucket={b.value: len(rows) for b, rows in bundle.members.items()},
        suppression_count=len(bundle.suppressed),
        merge_field_formats={
            "Zip code": dict(sorted(zip_formats.items(), key=lambda kv: -kv[1])),
            "Last purchase": dict(sorted(date_formats.items(), key=lambda kv: -kv[1])),
        },
        missing_fname=sum(1 for m in members if not m.fname.strip()),
        role_addresses=sum(1 for m in members if is_role_address(m.email)),
        segments=[
            SegmentSummary(
                name=s.name,
                match=s.match,
                condition_types=sorted({c.condition_type for c in s.conditions}),
                member_count=s.member_count,
            )
            for s in bundle.segments
        ],
        templates=[t.name for t in bundle.templates],
        campaigns_by_status=dict(Counter(c.status for c in bundle.campaigns)),
        merge_tags_in_use=sorted(merge_tags),
    )
