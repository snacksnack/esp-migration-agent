"""Deterministic migration risk register (RC1-544).

Severity-ranked risks detected at discovery time: features the target cannot
represent, data-quality problems, deliverability exposure, and known source-
platform export gaps. Phase 2's flagged mappings append to this same register
so the engagement carries one risk log end to end.

Detection is pure Python over the parsed bundle; no model judgment anywhere.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from discovery.inventory import Inventory
from ingest.models import ConsentBucket, MailchimpBundle

# What each supported target's segmentation can express. A condition type
# outside the set is an unsupported-feature risk, surfaced at discovery so it
# prices into the proposal instead of surprising the cutover.
SUPPORTED_SEGMENT_CONDITIONS: dict[str, frozenset[str]] = {
    # Listmonk segments subscribers by SQL over attributes; merge-field
    # conditions translate, Mailchimp-computed engagement ratings do not exist.
    "listmonk": frozenset({"TextMerge"}),
}


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


_SEVERITY_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


class Risk(BaseModel):
    code: str
    severity: Severity
    title: str
    detail: str
    count: int | None = None


class RiskError(RuntimeError):
    pass


def sort_risks(risks: list[Risk]) -> list[Risk]:
    """One severity-ranked log, however many sources contribute (discovery
    detections, and from RC1-545 the flagged-mapping open questions)."""
    return sorted(risks, key=lambda r: _SEVERITY_ORDER[r.severity])


def build_risk_register(
    bundle: MailchimpBundle, inventory: Inventory, target: str
) -> list[Risk]:
    if target not in SUPPORTED_SEGMENT_CONDITIONS:
        raise RiskError(f"unknown target {target!r}; known: {sorted(SUPPORTED_SEGMENT_CONDITIONS)}")
    supported = SUPPORTED_SEGMENT_CONDITIONS[target]
    risks: list[Risk] = []

    for seg in bundle.segments:
        unsupported = sorted({c.condition_type for c in seg.conditions} - supported)
        if unsupported:
            risks.append(
                Risk(
                    code="unsupported_segment",
                    severity=Severity.HIGH,
                    title=f"Segment {seg.name!r} cannot be represented in {target}",
                    detail=(
                        f"Condition type(s) {', '.join(unsupported)} have no {target} "
                        "equivalent. Needs a human decision: rebuild from a proxy "
                        "attribute, migrate as a static list, or drop."
                    ),
                )
            )

    risks += _breadth_risks(bundle, inventory)

    # Known Mailchimp export gap, observed while freezing the fixture: the
    # audience export ships no file for status=pending (fixtures/README.md).
    risks.append(
        Risk(
            code="pending_members_omitted",
            severity=Severity.HIGH,
            title="Audience export omits pending (unconfirmed) members",
            detail=(
                "Mailchimp audience exports carry no pending members, so a CSV-only "
                "migration silently drops unconfirmed signups. Reconcile exported row "
                "counts against the API list stats before cutover."
            ),
        )
    )

    zip_bad = sum(
        n for fmt, n in inventory.merge_field_formats["Zip code"].items()
        if fmt not in ("zip_5_digit", "empty")
    )
    if zip_bad:
        risks.append(
            Risk(
                code="malformed_zip",
                severity=Severity.MEDIUM,
                title="Malformed zip codes",
                detail=(
                    "Truncated (lost leading zero) or free-text values in 'Zip code'. "
                    "Geographic segments built on this field will mis-target."
                ),
                count=zip_bad,
            )
        )

    date_bad = sum(
        n for fmt, n in inventory.merge_field_formats["Last purchase"].items()
        if fmt not in ("iso_date", "empty")
    )
    if date_bad:
        risks.append(
            Risk(
                code="inconsistent_date_format",
                severity=Severity.MEDIUM,
                title="Mixed formats in 'Last purchase'",
                detail=(
                    "Non-ISO date formats and prose values. The field needs a parse-"
                    "and-normalize rule (or an explicit decision to migrate as text)."
                ),
                count=date_bad,
            )
        )

    if inventory.role_addresses:
        risks.append(
            Risk(
                code="role_addresses",
                severity=Severity.MEDIUM,
                title="Role addresses on the list",
                detail=(
                    "Shared inboxes (info@, postmaster@, ...) bounce and complain at "
                    "elevated rates; review before the first send from the target."
                ),
                count=inventory.role_addresses,
            )
        )

    ratings = {m.member_rating for m in bundle.all_members}
    if len(ratings) == 1:
        risks.append(
            Risk(
                code="no_engagement_signal",
                severity=Severity.MEDIUM,
                title="Engagement distribution is degenerate",
                detail=(
                    f"Every member has member_rating={ratings.pop()}: the source "
                    "account carries no usable engagement signal. Engagement-based "
                    "segments are empty and a warm-up ramp cannot be cohorted on it."
                ),
            )
        )

    if inventory.missing_fname:
        risks.append(
            Risk(
                code="missing_first_name",
                severity=Severity.LOW,
                title="Members with no first name",
                detail=(
                    "Personalized templates (*|FNAME|*) render blank for these "
                    "members; set a default greeting in the target."
                ),
                count=inventory.missing_fname,
            )
        )

    return sort_risks(risks)


def _breadth_risks(bundle: MailchimpBundle, inventory: Inventory) -> list[Risk]:
    """Limits the RC1-572 breadth corpora expose. Each is surfaced at
    discovery so it prices into the engagement instead of failing at cutover
    (ADR-0017)."""
    risks: list[Risk] = []

    if bundle.extra_columns:
        columns = ", ".join(bundle.extra_columns)
        risks.append(
            Risk(
                code="unmigrated_custom_fields",
                severity=Severity.HIGH,
                title="Custom merge fields the executor cannot migrate yet",
                detail=(
                    f"Data-bearing column(s) outside the standard export set: {columns}. "
                    "They reach the mapping proposal so a reviewer decides each one, but "
                    "the executor reads a fixed field set and refuses a spec that maps "
                    "them - extending the parser is a scoped engagement item."
                ),
                count=len(bundle.extra_columns),
            )
        )

    list_ids = sorted({c.list_id for c in bundle.campaigns})
    if len(list_ids) > 1:
        risks.append(
            Risk(
                code="multiple_audiences",
                severity=Severity.HIGH,
                title="The account has more than one audience",
                detail=(
                    f"Campaigns target {len(list_ids)} audiences ({', '.join(list_ids)}); "
                    "an audience export carries one. Each audience needs its own export, "
                    "review, and destination list - migrating two into one list merges "
                    "them, and an address in both may carry different consent in each."
                ),
                count=len(list_ids),
            )
        )
    elif list_ids:
        # One audience: an unsegmented campaign's recipient count is the
        # source's own count of mailable members. A shortfall in the export
        # (a row-boundary truncation parses cleanly) shows up only here.
        exported = len(bundle.members.get(ConsentBucket.SUBSCRIBED, []))
        for c in bundle.campaigns:
            if c.saved_segment_id is None and c.recipient_count != exported:
                risks.append(
                    Risk(
                        code="export_row_count_mismatch",
                        severity=Severity.HIGH,
                        title="Exported subscribers disagree with the source's own count",
                        detail=(
                            f"Campaign {c.title!r} reaches {c.recipient_count:,} members "
                            f"but the export carries {exported:,} subscribed rows. A "
                            "truncated or partial export parses cleanly; re-export and "
                            "compare before proposing anything."
                        ),
                        count=abs(c.recipient_count - exported),
                    )
                )
                break

    complex_tags = [t for t in inventory.merge_tags_in_use if ":" in t]
    if complex_tags:
        risks.append(
            Risk(
                code="complex_merge_tags",
                severity=Severity.MEDIUM,
                title="Conditional or function merge tags in campaign content",
                detail=(
                    f"Tags {', '.join(complex_tags)} are logic, not field lookups. Each "
                    "needs a reviewed translation into the target's template language; "
                    "the plan refuses any campaign with an untranslated tag."
                ),
                count=len(complex_tags),
            )
        )

    return risks
