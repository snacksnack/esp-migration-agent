"""Deterministic parsers for the Mailchimp export bundle (RC1-544).

Reads the audience export CSVs (subscribed/unsubscribed/nonsubscribed) and
the API supplement JSONs (segment definitions with conditions, template and
draft-campaign metadata) into typed models. No LLM anywhere in this module:
counting and parsing are deterministic by design (estate thesis).

Malformed input fails loudly with IngestError naming the file, column, or
row - never a silent zero (source-health convention).

Usage: bundle = load_bundle(audience_dir, supplement_dir)
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from ingest.models import (
    CampaignMeta,
    ConsentBucket,
    MailchimpBundle,
    SegmentDef,
    SubscriberRecord,
    TemplateMeta,
)

REQUIRED_COLUMNS = [
    "Email Address",
    "First Name",
    "Last Name",
    "Zip code",
    "State",
    "Last purchase",
    "MEMBER_RATING",
    "OPTIN_TIME",
    "CONFIRM_TIME",
]

# System/metadata columns Mailchimp ships in every audience export: not merge
# fields, never migration data. Anything OUTSIDE this set and REQUIRED_COLUMNS
# that carries data is a custom merge field the fixed parser cannot migrate -
# collected by extra_merge_columns() so it surfaces instead of silently
# dropping (RC1-572).
METADATA_COLUMNS = frozenset(
    {
        "OPTIN_IP",
        "CONFIRM_IP",
        "GMTOFF",
        "DSTOFF",
        "TIMEZONE",
        "CC",
        "REGION",
        "LAST_CHANGED",
        "LEID",
        "EUID",
        "NOTES",
        "TAGS",
        "SOURCE",
        "UNSUB_TIME",
        "UNSUB_CAMPAIGN_TITLE",
        "UNSUB_CAMPAIGN_ID",
        "UNSUB_REASON",
        "UNSUB_REASON_OTHER",
    }
)

EXTRA_SAMPLES_PER_COLUMN = 10
EXTRA_DISTINCT_CAP = 1000  # bounds memory on high-cardinality columns

CSV_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
MERGE_TAG_RE = re.compile(r"\*\|([A-Z0-9_:]+)\|\*")

# Mailchimp renders export timestamps in the ACCOUNT's timezone with no
# offset in the value - verified against the fixture's seeded UTC signups
# (exactly -4h/-5h, i.e. EDT/EST). Parsers must localize and convert back to
# UTC or every tenure computation drifts at cohort boundaries. The default is
# the fixture account's timezone; real engagements pass the customer's.
DEFAULT_ACCOUNT_TZ = "America/New_York"


class IngestError(RuntimeError):
    """Named parse failure: which file, which row or column, what was wrong."""


def _parse_time(
    value: str, column: str, path: Path, row_num: int, tz: ZoneInfo
) -> datetime | None:
    if not value or value == "n/a":
        return None
    try:
        return datetime.strptime(value, CSV_TIME_FORMAT).replace(tzinfo=tz).astimezone(UTC)
    except ValueError as exc:
        raise IngestError(f"{path.name} row {row_num}: bad {column} {value!r}") from exc


def _parse_member_row(
    row: dict[str, str], bucket: ConsentBucket, path: Path, row_num: int, tz: ZoneInfo
) -> SubscriberRecord:
    # Every export row carries a cell for every header column, empty or not.
    # DictReader marks a short row's missing cells None and files surplus
    # cells under the None key: either shape is a truncated or malformed
    # row, even when every REQUIRED field happens to be intact (RC1-572).
    if None in row:
        raise IngestError(
            f"{path.name} row {row_num}: more cells than the header has columns"
        )
    missing = sum(1 for value in row.values() if value is None)
    if missing:
        raise IngestError(
            f"{path.name} row {row_num}: {missing} trailing column(s) missing "
            "(truncated or malformed row)"
        )
    email = (row.get("Email Address") or "").strip()
    if not email:
        raise IngestError(f"{path.name} row {row_num}: empty Email Address")
    rating_raw = row.get("MEMBER_RATING") or ""
    try:
        rating = int(rating_raw)
    except ValueError as exc:
        raise IngestError(f"{path.name} row {row_num}: bad MEMBER_RATING {rating_raw!r}") from exc
    try:
        return SubscriberRecord(
            email=email.lower(),
            bucket=bucket,
            fname=row["First Name"],
            lname=row["Last Name"],
            zip_code=row["Zip code"],
            state=row["State"],
            last_purchase=row["Last purchase"],
            member_rating=rating,
            optin_time=_parse_time(row["OPTIN_TIME"], "OPTIN_TIME", path, row_num, tz),
            confirm_time=_parse_time(row["CONFIRM_TIME"], "CONFIRM_TIME", path, row_num, tz),
            unsub_time=_parse_time(row.get("UNSUB_TIME") or "", "UNSUB_TIME", path, row_num, tz),
        )
    except ValidationError as exc:
        # A truncated row leaves trailing cells as None; name the row rather
        # than leaking a pydantic stack (RC1-572).
        raise IngestError(f"{path.name} row {row_num}: incomplete or malformed row: {exc}") from exc


def load_audience_csv(
    path: Path, bucket: ConsentBucket, account_tz: str = DEFAULT_ACCOUNT_TZ
) -> list[SubscriberRecord]:
    tz = ZoneInfo(account_tz)
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                raise IngestError(f"{path.name}: missing expected columns {missing}")
            return [
                _parse_member_row(row, bucket, path, row_num, tz)
                for row_num, row in enumerate(reader, 2)
            ]
    except UnicodeDecodeError as exc:
        raise IngestError(
            f"{path.name}: not valid UTF-8 (wrong encoding, or a truncated/"
            f"corrupted export): {exc}"
        ) from exc
    except csv.Error as exc:
        raise IngestError(f"{path.name}: malformed CSV: {exc}") from exc


def extra_merge_columns(paths: list[Path]) -> dict[str, list[str]]:
    """Custom merge-field columns outside the standard export set that carry
    data, with the first EXTRA_SAMPLES_PER_COLUMN distinct raw values in sorted
    order (deterministic whatever the row order). The fixed
    parser cannot migrate these; they must surface, never silently drop
    (RC1-572). Mailchimp's standard-but-empty audience fields (Address,
    Birthday, ...) stay invisible until a value appears in one."""
    known = set(REQUIRED_COLUMNS) | METADATA_COLUMNS
    distinct: dict[str, set[str]] = {}
    for path in paths:
        try:
            with path.open(newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                candidates = [c for c in (reader.fieldnames or []) if c not in known]
                if not candidates:
                    continue
                for row in reader:
                    for column in candidates:
                        value = row.get(column) or ""
                        if not value.strip():
                            continue
                        # Raw, not stripped: padding is one of the formats a
                        # reviewer (and the proposer) has to see.
                        seen = distinct.setdefault(column, set())
                        if len(seen) < EXTRA_DISTINCT_CAP:
                            seen.add(value)
        except (UnicodeDecodeError, csv.Error) as exc:
            raise IngestError(f"{path.name}: unreadable while scanning columns: {exc}") from exc
    return {
        column: sorted(values)[:EXTRA_SAMPLES_PER_COLUMN]
        for column, values in sorted(distinct.items())
    }


def load_audience_dir(
    audience_dir: Path, account_tz: str = DEFAULT_ACCOUNT_TZ
) -> tuple[dict[ConsentBucket, list[SubscriberRecord]], dict[str, list[str]]]:
    """Load the three per-consent-bucket CSVs a Mailchimp audience export
    ships, plus any data-bearing custom merge columns the fixed parser
    cannot migrate (RC1-572)."""
    members: dict[ConsentBucket, list[SubscriberRecord]] = {}
    paths: list[Path] = []
    for bucket in ConsentBucket:
        matches = sorted(audience_dir.glob(f"{bucket.value}_*.csv"))
        if len(matches) != 1:
            raise IngestError(
                f"{audience_dir}: expected exactly one {bucket.value}_*.csv, "
                f"found {len(matches)}"
            )
        members[bucket] = load_audience_csv(matches[0], bucket, account_tz)
        if not members[bucket]:
            raise IngestError(f"{matches[0].name}: no member rows")
        paths.append(matches[0])
    _refuse_duplicate_emails(members)
    return members, extra_merge_columns(paths)


def _refuse_duplicate_emails(
    members: dict[ConsentBucket, list[SubscriberRecord]],
) -> None:
    """An address appearing twice is a malformed export, and across buckets it
    is a consent conflict the plan would act on twice - phase 1 suppresses,
    phase 2 upserts right back. No automatic winner: a human resolves the
    export (RC1-563)."""
    buckets_by_email: dict[str, list[str]] = {}
    for bucket, records in members.items():
        for record in records:
            buckets_by_email.setdefault(record.email, []).append(bucket.value)
    conflicts = {
        email: buckets for email, buckets in buckets_by_email.items() if len(buckets) > 1
    }
    if conflicts:
        shown = ", ".join(
            f"{email} ({'+'.join(buckets)})"
            for email, buckets in sorted(conflicts.items())[:5]
        )
        more = f" and {len(conflicts) - 5} more" if len(conflicts) > 5 else ""
        raise IngestError(
            f"{len(conflicts)} address(es) appear more than once across the "
            f"audience export: {shown}{more}. Consent is ambiguous; resolve "
            "the export before migrating (no bucket silently wins)."
        )


def _load_json(path: Path) -> list[dict]:
    if not path.is_file():
        raise IngestError(f"{path}: missing")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise IngestError(f"{path.name}: invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise IngestError(f"{path.name}: expected a JSON array")
    return data


def load_segments(supplement_dir: Path) -> list[SegmentDef]:
    segments = []
    for raw in _load_json(supplement_dir / "segments.json"):
        options = raw.get("options") or {}
        try:
            segments.append(
                SegmentDef(
                    id=raw["id"],
                    name=raw["name"],
                    type=raw["type"],
                    match=options.get("match", ""),
                    conditions=options.get("conditions", []),
                    member_count=raw["member_count"],
                )
            )
        except (KeyError, ValidationError) as exc:
            raise IngestError(f"segments.json: bad segment {raw.get('name')!r}: {exc}") from exc
    return segments


def load_templates(supplement_dir: Path) -> list[TemplateMeta]:
    try:
        return [TemplateMeta(**raw) for raw in _load_json(supplement_dir / "templates.json")]
    except ValidationError as exc:
        raise IngestError(f"templates.json: bad template entry: {exc}") from exc


def load_campaigns(supplement_dir: Path) -> list[CampaignMeta]:
    campaigns = []
    for raw in _load_json(supplement_dir / "campaigns.json"):
        settings = raw.get("settings") or {}
        recipients = raw.get("recipients") or {}
        html = (raw.get("content") or {}).get("html", "")
        try:
            campaigns.append(
                CampaignMeta(
                    id=raw["id"],
                    status=raw["status"],
                    type=raw["type"],
                    title=settings["title"],
                    subject_line=settings["subject_line"],
                    from_name=settings["from_name"],
                    list_id=recipients["list_id"],
                    recipient_count=recipients["recipient_count"],
                    saved_segment_id=(recipients.get("segment_opts") or {}).get(
                        "saved_segment_id"
                    ),
                    html=html,
                    merge_tags=sorted(set(MERGE_TAG_RE.findall(html))),
                )
            )
        except (KeyError, ValidationError) as exc:
            raise IngestError(f"campaigns.json: bad campaign {raw.get('id')!r}: {exc}") from exc
    return campaigns


def bundle_fingerprint(audience_dir: Path, supplement_dir: Path) -> str:
    """sha256 over every file the bundle is parsed from, sorted by name: the
    source identity a spec's approval pins, so the executor only runs against
    the exact export that was reviewed (RC1-566)."""
    digest = hashlib.sha256()
    for directory in (audience_dir, supplement_dir):
        for path in sorted(p for p in directory.iterdir() if p.is_file()):
            digest.update(path.name.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def load_bundle(
    audience_dir: Path, supplement_dir: Path, account_tz: str = DEFAULT_ACCOUNT_TZ
) -> MailchimpBundle:
    members, extra_columns = load_audience_dir(audience_dir, account_tz)
    return MailchimpBundle(
        members=members,
        segments=load_segments(supplement_dir),
        templates=load_templates(supplement_dir),
        campaigns=load_campaigns(supplement_dir),
        extra_columns=extra_columns,
    )
