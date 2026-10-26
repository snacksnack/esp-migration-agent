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
from ingest.models import MailchimpBundle

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
