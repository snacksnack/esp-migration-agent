"""Deterministic migration plan (RC1-546): approved spec + bundle -> plan.

Everything here is pure Python over validated inputs. The plan is built in
full - every record transformed, every consent label resolved - BEFORE any
write happens, and the suppression invariant is checked during that build:
a suppressed address whose resolved consent is subscribable raises
SuppressionViolationError and nothing executes. A hostile approved spec
(e.g. unsubscribed -> enabled/confirmed) cannot get past this, which is the
RC1-546 acceptance test.

The plan's phase order is structural: suppressions are a separate, first
phase; the executor iterates phases in order and the write loop re-checks the
invariant per record (belt and braces).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from pydantic import BaseModel

from ingest.models import ConsentBucket, MailchimpBundle, SubscriberRecord
from specs.model import MappingSpec, MappingStatus, SpecStatus

MERGE_TAG_RE = re.compile(r"\*\|([A-Z0-9_:]+)\|\*")


class PlanError(RuntimeError):
    """The spec and the bundle do not line up; nothing was written."""


class SuppressionViolationError(PlanError):
    """THE invariant: a suppressed address would land subscribable. The spec
    is inconsistent with the suppression policy; refuse loudly, never
    silently override (the reviewer must see and fix the spec)."""


# --- field transforms --------------------------------------------------------

def _zero_pad_us_zip(value: str) -> str:
    return value.zfill(5) if value.isdigit() and len(value) < 5 else value


TRANSFORMS: dict[str, Callable[[str], str]] = {
    "none": lambda v: v,
    "lowercase": str.lower,
    "zero_pad_us_zip": _zero_pad_us_zip,
}


# --- target consent dialects -------------------------------------------------

class ConsentState(BaseModel):
    """What a consent label means in the target: subscriber status, per-list
    subscription status, and - for the invariant - whether the address could
    ever receive marketing mail from this state."""

    status: str
    subscription: str
    subscribable: bool


# Canonical labels a reviewed ConsentMapping.target_status may use, per
# target. `subscribable` is strict: unconfirmed counts as subscribable
# because a confirm click away is not suppressed.
CONSENT_DIALECTS: dict[str, dict[str, ConsentState]] = {
    "listmonk": {
        "enabled/confirmed": ConsentState(
            status="enabled", subscription="confirmed", subscribable=True
        ),
        "enabled/unconfirmed": ConsentState(
            status="enabled", subscription="unconfirmed", subscribable=True
        ),
        "unsubscribed": ConsentState(
            status="enabled", subscription="unsubscribed", subscribable=False
        ),
        "blocklisted": ConsentState(
            status="blocklisted", subscription="unsubscribed", subscribable=False
        ),
    },
}

# Which source-record attribute each mappable source field reads from.
SOURCE_FIELD_READERS: dict[str, Callable[[SubscriberRecord], str | int | None]] = {
    "EMAIL": lambda m: m.email,
    "FNAME": lambda m: m.fname,
    "LNAME": lambda m: m.lname,
    "ZIP": lambda m: m.zip_code,
    "STATE": lambda m: m.state,
    "LASTPURCH": lambda m: m.last_purchase,
    "MEMBER_RATING": lambda m: m.member_rating,
    "OPTIN_TIME": lambda m: m.optin_time.isoformat() if m.optin_time else None,
    "CONFIRM_TIME": lambda m: m.confirm_time.isoformat() if m.confirm_time else None,
}


def target_state_subscribable(target: str, status: str, subscription: str) -> bool:
    """Whether an EXISTING target state could receive marketing mail, by the
    same dialect table the plan writes with. Unknown states count as
    subscribable (unprotected): only a state the dialect knows is protected
    may veto a write (RC1-568)."""
    for state in CONSENT_DIALECTS.get(target, {}).values():
        if (state.status, state.subscription) == (status, subscription):
            return state.subscribable
    return True


# --- plan models -------------------------------------------------------------

class DesiredSubscriber(BaseModel):
    email: str
    name: str
    status: str
    subscription: str
    subscribable: bool
    suppressed: bool  # member of the suppression set (migrates in phase 1)
    attribs: dict[str, str | int]


class PlannedSegment(BaseModel):
    name: str
    query: str
    source_member_count: int


class PlannedCampaign(BaseModel):
    name: str
    subject: str
    body: str  # merge tags already translated to the target syntax


class MigrationPlan(BaseModel):
    source: str
    target: str
    list_name: str
    suppressions: list[DesiredSubscriber]  # phase 1, always first
    subscribers: list[DesiredSubscriber]  # phase 2
    campaigns: list[PlannedCampaign]  # phase 3 (drafts; nothing sends)
    segments: list[PlannedSegment]  # verification queries, not target objects

    @property
    def all_subscribers(self) -> list[DesiredSubscriber]:
        return [*self.suppressions, *self.subscribers]


# --- translation helpers -----------------------------------------------------

def translate_merge_tags(html: str, tag_map: dict[str, str], context: str) -> str:
    translated = MERGE_TAG_RE.sub(
        lambda match: tag_map.get(match.group(1), match.group(0)), html
    )
    residual = sorted(set(MERGE_TAG_RE.findall(translated)))
    if residual:
        raise PlanError(
            f"{context}: merge tag(s) {', '.join(residual)} have no mapping in the "
            "approved spec; refusing to migrate content with untranslated tags"
        )
    return translated


def _attrib_key(target_field: str, source_field: str) -> str | None:
    """Resolve a field mapping's target path: 'email' is the identity column,
    'attribs.<key>' is a custom attribute. Anything else is a spec defect."""
    if target_field == "email":
        return None
    if target_field.startswith("attribs."):
        return target_field.removeprefix("attribs.")
    raise PlanError(
        f"field {source_field!r} maps to unknown target path {target_field!r} "
        "(expected 'email' or 'attribs.<key>')"
    )


# --- the builder -------------------------------------------------------------

def build_plan(bundle: MailchimpBundle, spec: MappingSpec) -> MigrationPlan:
    if spec.status is not SpecStatus.APPROVED:
        raise PlanError(
            f"spec status is {spec.status}; the executor only plans from an "
            "approved spec (load it via mapping.gate.load_approved)"
        )
    if spec.target not in CONSENT_DIALECTS:
        raise PlanError(f"no consent dialect for target {spec.target!r}")
    dialect = CONSENT_DIALECTS[spec.target]

    consent_by_bucket: dict[str, ConsentState] = {}
    for mapping in spec.consent_mappings:
        if mapping.target_status not in dialect:
            raise PlanError(
                f"consent mapping {mapping.source_status!r} -> "
                f"{mapping.target_status!r} is not a {spec.target} consent label "
                f"(known: {sorted(dialect)})"
            )
        consent_by_bucket[mapping.source_status] = dialect[mapping.target_status]
    observed = {bucket.value for bucket in bundle.members}
    unmapped = observed - set(consent_by_bucket)
    if unmapped:
        raise PlanError(
            f"observed consent bucket(s) {sorted(unmapped)} have no consent "
            "mapping in the approved spec; nothing may be silently dropped"
        )

    field_mappings = [
        m for m in spec.field_mappings if m.status is not MappingStatus.FLAGGED
    ]
    for m in field_mappings:
        if m.source_field not in SOURCE_FIELD_READERS:
            raise PlanError(f"unknown source field {m.source_field!r} in spec")
        if m.transform not in TRANSFORMS:
            raise PlanError(
                f"field {m.source_field!r} uses unknown transform {m.transform!r} "
                f"(known: {sorted(TRANSFORMS)})"
            )

    suppressed_buckets = set(spec.suppressions.source_statuses)

    def desired(member: SubscriberRecord) -> DesiredSubscriber:
        consent = consent_by_bucket[member.bucket.value]
        suppressed = member.bucket.value in suppressed_buckets
        if suppressed and consent.subscribable:
            raise SuppressionViolationError(
                f"approved spec maps suppressed bucket {member.bucket.value!r} to a "
                "subscribable consent state; no suppressed address may land "
                "subscribable. Fix the spec and re-approve - the executor will not "
                "override or proceed."
            )
        email = member.email
        attribs: dict[str, str | int] = {}
        for m in field_mappings:
            raw = SOURCE_FIELD_READERS[m.source_field](member)
            # An empty source value is NO value: the key is omitted, on every
            # target. (Brevo drops empty-string attributes on write, so
            # writing "" would never converge; Listmonk would store it, and
            # the two targets would disagree about the same spec.)
            if raw is None or raw == "":
                continue
            value = TRANSFORMS[m.transform](raw) if isinstance(raw, str) else raw
            key = _attrib_key(m.target_field, m.source_field)
            if key is None:
                email = str(value)
            else:
                attribs[key] = value
        return DesiredSubscriber(
            email=email,
            name=f"{member.fname} {member.lname}".strip(),
            status=consent.status,
            subscription=consent.subscription,
            subscribable=consent.subscribable,
            suppressed=suppressed,
            attribs=attribs,
        )

    suppressions: list[DesiredSubscriber] = []
    subscribers: list[DesiredSubscriber] = []
    for bucket in ConsentBucket:
        for member in bundle.members.get(bucket, []):
            record = desired(member)
            (suppressions if record.suppressed else subscribers).append(record)

    tag_map = {m.source_tag.strip("*|"): m.target_tag for m in spec.merge_tag_mappings}
    campaigns = [
        PlannedCampaign(
            name=c.title,
            subject=c.subject_line,
            # Edge whitespace is stripped: targets normalize it on store
            # (Brevo drops a trailing newline), and a body that differs only
            # there must still converge.
            body=translate_merge_tags(
                c.html, tag_map, context=f"campaign {c.title!r}"
            ).strip(),
        )
        for c in bundle.campaigns
    ]

    segments = [
        PlannedSegment(
            name=m.source_segment,
            query=m.target_query or "",
            source_member_count=next(
                (s.member_count for s in bundle.segments if s.name == m.source_segment), 0
            ),
        )
        for m in spec.segment_mappings
        if m.target_query
    ]

    return MigrationPlan(
        source=spec.source,
        target=spec.target,
        list_name=f"Imported audience ({spec.source})",
        suppressions=suppressions,
        subscribers=subscribers,
        campaigns=campaigns,
        segments=segments,
    )
