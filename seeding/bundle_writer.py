"""Synthetic Mailchimp export-bundle writer (RC1-572; grows toward RC1-555).

Renders generator records into the exact file shapes the genuine fixture was
captured in (ADR-0017): the three per-bucket audience CSVs with Mailchimp's
full export column set, and the API-supplement JSONs in the shape
`seeding/capture_supplement.py` saves them. The inverse of ingest, validated
by round-trip tests against the generator's ground truth - so breadth corpora
(multi-audience, custom merge fields, malformed variants) exist without
burning a live Mailchimp capture.

Fidelity notes, matched to the genuine capture:
- Timestamps render in the ACCOUNT timezone with no offset (the parser
  localizes them back; ADR-0004).
- Members with status "pending" appear in NO file - the writer reproduces the
  real export gap the risk register reports.
- The unsubscribed file carries the UNSUB_* columns and no LAST_CHANGED;
  the other two carry LAST_CHANGED and no UNSUB_* (header-for-header equal
  to the capture, enforced by tests/test_corpora.py).
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ingest.mailchimp import CSV_TIME_FORMAT, DEFAULT_ACCOUNT_TZ
from seeding.generator import Subscriber

# The genuine capture's column order (fixtures/mailchimp_export/audience).
BASE_COLUMNS = [
    "Email Address", "First Name", "Last Name", "Address", "Phone Number",
    "Birthday", "Company", "Zip code", "State", "Last purchase",
    "MEMBER_RATING", "OPTIN_TIME", "OPTIN_IP", "CONFIRM_TIME", "CONFIRM_IP",
    "GMTOFF", "DSTOFF", "TIMEZONE", "CC", "REGION",
]
UNSUB_COLUMNS = [
    "UNSUB_TIME", "UNSUB_CAMPAIGN_TITLE", "UNSUB_CAMPAIGN_ID",
    "UNSUB_REASON", "UNSUB_REASON_OTHER",
]
TAIL_COLUMNS = ["LEID", "EUID", "NOTES", "TAGS", "SOURCE"]

MASKED_IP = "203.0.113.35"  # TEST-NET-3, as in the genuine capture

# generator status -> export bucket file; "pending" maps to None on purpose:
# the real audience export ships no file for it (fixtures/README.md).
STATUS_TO_BUCKET = {
    "subscribed": "subscribed",
    "unsubscribed": "unsubscribed",
    "transactional": "nonsubscribed",
    "pending": None,
}


def _ts(value: datetime, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime(CSV_TIME_FORMAT)


def _row(sub: Subscriber, index: int, bucket: str, tz: ZoneInfo,
         custom_fields: tuple[str, ...]) -> dict[str, str]:
    stamp = _ts(sub.signup_at, tz)
    row = {
        "Email Address": sub.email,
        "First Name": sub.fname,
        "Last Name": sub.lname,
        "Zip code": sub.zip_code,
        "State": sub.state,
        "Last purchase": sub.last_purch,
        "MEMBER_RATING": "2",  # matches the fixture's degenerate distribution
        "OPTIN_TIME": stamp,
        "OPTIN_IP": MASKED_IP,
        "CONFIRM_TIME": stamp,
        "LEID": str(600000000 + index),
        "EUID": f"{index:010x}",
        "SOURCE": "API - synthetic bundle_writer",
    }
    if bucket == "unsubscribed":
        row["UNSUB_TIME"] = _ts(sub.signup_at + timedelta(days=45), tz)
        row["UNSUB_REASON"] = "manual unsubscribe"
    else:
        row["LAST_CHANGED"] = stamp
    for field in custom_fields:
        row[field] = sub.custom.get(field, "")
    return row


def write_audience_csvs(
    subs: list[Subscriber],
    audience_dir: Path,
    *,
    export_id: str = "5720000000",
    account_tz: str = DEFAULT_ACCOUNT_TZ,
    custom_fields: tuple[str, ...] = (),
) -> None:
    """The three per-bucket CSVs, exactly as an audience export names and
    shapes them. Pending members are omitted, as the real export omits them."""
    tz = ZoneInfo(account_tz)
    by_bucket: dict[str, list[Subscriber]] = {
        "subscribed": [], "unsubscribed": [], "nonsubscribed": []
    }
    for sub in subs:
        bucket = STATUS_TO_BUCKET[sub.status]
        if bucket is not None:
            by_bucket[bucket].append(sub)

    audience_dir.mkdir(parents=True, exist_ok=True)
    for bucket, rows in by_bucket.items():
        # Per the genuine capture: unsubscribed carries UNSUB_* and NO
        # LAST_CHANGED; the other two carry LAST_CHANGED and no UNSUB_*.
        columns = list(BASE_COLUMNS)
        columns += UNSUB_COLUMNS if bucket == "unsubscribed" else ["LAST_CHANGED"]
        columns += TAIL_COLUMNS + list(custom_fields)
        path = audience_dir / f"{bucket}_email_audience_export_{export_id}.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns, restval="")
            writer.writeheader()
            for index, sub in enumerate(rows):
                writer.writerow(_row(sub, index, bucket, tz, custom_fields))


def make_segment(seg_id: int, name: str, conditions: list[dict],
                 member_count: int, match: str = "all") -> dict:
    return {
        "id": seg_id,
        "name": name,
        "type": "saved",
        "options": {"match": match, "conditions": conditions},
        "member_count": member_count,
    }


def make_campaign(campaign_id: str, title: str, subject: str, html: str,
                  list_id: str, recipient_count: int,
                  saved_segment_id: int | None = None, list_name: str = "") -> dict:
    recipients: dict = {"list_id": list_id, "recipient_count": recipient_count}
    if list_name:  # the audience name, as the captured supplement carries it
        recipients["list_name"] = list_name
    if saved_segment_id is not None:
        recipients["segment_opts"] = {"saved_segment_id": saved_segment_id}
    return {
        "id": campaign_id,
        "status": "save",
        "type": "regular",
        "settings": {
            "title": title,
            "subject_line": subject,
            "from_name": "Synthetic Dispatch",
        },
        "recipients": recipients,
        "content": {"html": html},
    }


def write_supplement(
    supplement_dir: Path,
    *,
    segments: list[dict] | None = None,
    templates: list[dict] | None = None,
    campaigns: list[dict] | None = None,
) -> None:
    """segments/templates/campaigns JSONs in the captured supplement's shape."""
    supplement_dir.mkdir(parents=True, exist_ok=True)
    (supplement_dir / "segments.json").write_text(json.dumps(segments or [], indent=1))
    (supplement_dir / "templates.json").write_text(
        json.dumps(templates or [{"id": 1, "name": "Synthetic base", "type": "template"}], indent=1)
    )
    (supplement_dir / "campaigns.json").write_text(json.dumps(campaigns or [], indent=1))
