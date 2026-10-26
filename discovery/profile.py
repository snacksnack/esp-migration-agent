"""Deterministic audience profile (RC1-544).

Every number the discovery report's "who this audience is" section shows is
counted here, in Python, over the parsed subscriber records. The LLM narrates
these aggregates (discovery/narrator.py); it never computes them. The report
carries aggregates only - no record-level PII leaves this module.

Verified against seeding/ground_truth.json in tests/test_profile.py.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from pydantic import BaseModel

from ingest.models import MailchimpBundle, SubscriberRecord

# Public mailbox providers; everything else counts as corporate. On real
# engagements this list grows, but membership stays a deterministic lookup.
CONSUMER_DOMAINS = frozenset(
    {
        "gmail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "aol.com",
        "icloud.com",
        "live.com",
        "msn.com",
        "proton.me",
        "protonmail.com",
    }
)

# Tenure cohort upper bounds in days since signup, evaluated in order.
TENURE_COHORTS = [("lt_1y", 365), ("1_3y", 3 * 365), ("3_7y", 7 * 365)]
TENURE_OLDEST = "gt_7y"
TENURE_UNKNOWN = "unknown"

TOP_DOMAINS = 10


class ProfileError(RuntimeError):
    pass


class AudienceProfile(BaseModel):
    """Aggregates only; no record-level fields."""

    as_of: datetime
    total: int
    consent: dict[str, int]  # per export bucket, as the source platform names them
    domain_class: dict[str, int]  # consumer | corporate
    top_domains: dict[str, int]
    states: dict[str, int]
    tenure_cohorts: dict[str, int]
    member_rating: dict[int, int]
    suppressed: int


def _sorted_counts(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def _tenure_cohort(record: SubscriberRecord, as_of: datetime) -> str:
    signup = record.confirm_time or record.optin_time
    if signup is None:
        return TENURE_UNKNOWN
    days = (as_of - signup).days
    for name, upper in TENURE_COHORTS:
        if days < upper:
            return name
    return TENURE_OLDEST


def build_profile(bundle: MailchimpBundle, as_of: datetime) -> AudienceProfile:
    if as_of.tzinfo is None:
        raise ProfileError("as_of must be timezone-aware (export timestamps are UTC)")
    members = bundle.all_members
    if not members:
        raise ProfileError("bundle has no members")

    domains = Counter(m.email.rsplit("@", 1)[1] for m in members)
    domain_class = Counter(
        "consumer" if m.email.rsplit("@", 1)[1] in CONSUMER_DOMAINS else "corporate"
        for m in members
    )
    return AudienceProfile(
        as_of=as_of,
        total=len(members),
        consent={bucket.value: len(rows) for bucket, rows in bundle.members.items()},
        domain_class=_sorted_counts(domain_class),
        top_domains=dict(
            sorted(domains.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_DOMAINS]
        ),
        states=_sorted_counts(Counter(m.state for m in members if m.state)),
        tenure_cohorts=_sorted_counts(Counter(_tenure_cohort(m, as_of) for m in members)),
        member_rating=dict(sorted(Counter(m.member_rating for m in members).items())),
        suppressed=len(bundle.suppressed),
    )
