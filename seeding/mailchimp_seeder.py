"""Seed the Mailchimp free account with generated subscribers (ADR-0001).

Run once against a fresh free account, then trigger the account export in the
Mailchimp UI (Account > Settings > Manage > Export account data) and commit
the bundle under fixtures/. Writes seeding/ground_truth.json as it seeds.

Usage: uv run python -m seeding.mailchimp_seeder [--count 450] [--seed 542] [--dry-run]

Needs ESP_MAILCHIMP_API_KEY (see .env.example). Fails loudly without it:
a missing credential is a named error, never a silent zero.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from seeding.generator import Subscriber, generate

GROUND_TRUTH_PATH = Path(__file__).parent / "ground_truth.json"

MERGE_FIELDS = [
    {"tag": "ZIP", "name": "Zip code", "type": "text"},
    {"tag": "STATE", "name": "State", "type": "text"},
    {"tag": "LASTPURCH", "name": "Last purchase", "type": "text"},
]

# Saved segments created at seed time so discovery (RC1-544) finds real segment
# definitions, including the engagement one Phase 2 must flag (no Listmonk
# equivalent for member_rating).
SEGMENTS = [
    {
        "name": "Brooklyn subscribers",
        "options": {
            "match": "all",
            "conditions": [
                {"condition_type": "TextMerge", "field": "ZIP", "op": "starts", "value": "112"}
            ],
        },
    },
    {
        "name": "Highly engaged (4-5 stars)",
        "options": {
            "match": "all",
            "conditions": [
                {"condition_type": "Rating", "field": "rating", "op": "greater", "value": 3}
            ],
        },
    },
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ESP_", env_file=".env", extra="ignore")

    mailchimp_api_key: str | None = None


class SeedError(RuntimeError):
    pass


def client_for(api_key: str) -> httpx.Client:
    if "-" not in api_key:
        raise SeedError("ESP_MAILCHIMP_API_KEY has no datacenter suffix (expected ...-usNN)")
    dc = api_key.rsplit("-", 1)[1]
    return httpx.Client(
        base_url=f"https://{dc}.api.mailchimp.com/3.0",
        auth=("anystring", api_key),
        timeout=30,
    )


def _check(resp: httpx.Response, what: str) -> dict:
    if resp.status_code >= 400:
        raise SeedError(f"{what} failed: HTTP {resp.status_code} {resp.text[:500]}")
    return resp.json()


def get_audience_id(client: httpx.Client) -> str:
    lists = _check(client.get("/lists"), "list audiences")["lists"]
    if not lists:
        raise SeedError(
            "no audience found - create one in the Mailchimp UI first "
            "(the free tier ships with exactly one)"
        )
    return lists[0]["id"]


def ensure_merge_fields(client: httpx.Client, list_id: str) -> None:
    existing = _check(client.get(f"/lists/{list_id}/merge-fields?count=50"), "list merge fields")
    tags = {f["tag"] for f in existing["merge_fields"]}
    for field in MERGE_FIELDS:
        if field["tag"] not in tags:
            _check(
                client.post(f"/lists/{list_id}/merge-fields", json=field),
                f"create merge field {field['tag']}",
            )


def upsert_member(client: httpx.Client, list_id: str, sub: Subscriber) -> None:
    sub_hash = hashlib.md5(sub.email.encode()).hexdigest()  # Mailchimp's member key scheme
    body = {
        "email_address": sub.email,
        "status": sub.status,
        "timestamp_signup": sub.signup_at.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "merge_fields": {
            "FNAME": sub.fname,
            "LNAME": sub.lname,
            "ZIP": sub.zip_code,
            "STATE": sub.state,
            "LASTPURCH": sub.last_purch,
        },
    }
    _check(client.put(f"/lists/{list_id}/members/{sub_hash}", json=body), f"upsert {sub.email}")


def create_segments(client: httpx.Client, list_id: str) -> None:
    existing = _check(client.get(f"/lists/{list_id}/segments?count=50"), "list segments")
    names = {s["name"] for s in existing["segments"]}
    for seg in SEGMENTS:
        if seg["name"] not in names:
            _check(client.post(f"/lists/{list_id}/segments", json=seg), f"segment {seg['name']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=450)
    parser.add_argument("--seed", type=int, default=542)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="generate and write ground_truth.json without touching Mailchimp",
    )
    args = parser.parse_args()

    subs, truth = generate(count=args.count, seed=args.seed)
    GROUND_TRUTH_PATH.write_text(truth.model_dump_json(indent=2) + "\n")
    print(f"ground truth written to {GROUND_TRUTH_PATH}")
    print(json.dumps({k: v for k, v in truth.model_dump().items() if k != "suppressed_emails"}))

    if args.dry_run:
        return 0

    settings = Settings()
    if not settings.mailchimp_api_key:
        raise SeedError("ESP_MAILCHIMP_API_KEY is not set (see .env.example)")

    with client_for(settings.mailchimp_api_key) as client:
        list_id = get_audience_id(client)
        ensure_merge_fields(client, list_id)
        for i, sub in enumerate(subs, 1):
            upsert_member(client, list_id, sub)
            if i % 50 == 0:
                print(f"  {i}/{len(subs)} members upserted")
        create_segments(client, list_id)

    print(f"seeded {len(subs)} members into audience {list_id}; "
          "now trigger the account export in the Mailchimp UI (see fixtures/README.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
