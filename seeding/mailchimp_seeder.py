"""Seed the Mailchimp free account with generated subscribers (ADR-0001).

Run once against a fresh free account, then trigger the account export in the
Mailchimp UI (Account > Settings > Manage > Export account data) and commit
the bundle under fixtures/. Writes seeding/ground_truth.json as it seeds.

Usage: uv run python -m seeding.mailchimp_seeder [--count 450] [--seed 542] [--dry-run]

Needs ESP_MAILCHIMP_API_KEY (see .env.example). Fails loudly without it:
a missing credential is a named error, never a silent zero.

Deliberately one idempotent PUT per member: safe to resume after a crash and
every rejection is attributable to one address. Sequential calls run at only
~2-3 members/second, so at a larger scale (RC1-555) switch to batch subscribe
(POST /lists/{list_id}, 500 members per call) or POST /batches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from seeding.generator import Subscriber, _aggregate, generate

GROUND_TRUTH_PATH = Path(__file__).parent / "ground_truth.json"
REJECTED_PATH = Path(__file__).parent / "rejected_emails.json"

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
                {"condition_type": "MemberRating", "field": "rating", "op": "greater", "value": 3}
            ],
        },
    },
    # Deliberate RC1-545 ambiguity: two segments that both claim to be "the
    # VIP list", with overlapping-but-different membership. Each condition is
    # TextMerge (representable in Listmonk), so the open question the mapper
    # must raise is consolidation intent, not representability. Note both lean
    # on LASTPURCH, the mixed-format free-text date field - a text `contains`
    # condition over it silently misses non-ISO rows, which compounds the
    # ambiguity exactly the way real accounts do.
    {
        "name": "VIP customers",
        "options": {
            "match": "all",
            "conditions": [
                {
                    "condition_type": "TextMerge",
                    "field": "LASTPURCH",
                    "op": "contains",
                    "value": "2026",
                }
            ],
        },
    },
    {
        "name": "VIP (legacy)",
        "options": {
            "match": "any",
            "conditions": [
                {
                    "condition_type": "TextMerge",
                    "field": "LASTPURCH",
                    "op": "contains",
                    "value": "2026",
                },
                {"condition_type": "TextMerge", "field": "ZIP", "op": "starts", "value": "112"},
            ],
        },
    },
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ESP_", env_file=".env", extra="ignore")

    mailchimp_api_key: str | None = None


class SeedError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def client_for(api_key: str) -> httpx.Client:
    if "-" not in api_key:
        raise SeedError("ESP_MAILCHIMP_API_KEY has no datacenter suffix (expected ...-usNN)")
    dc = api_key.rsplit("-", 1)[1]
    return httpx.Client(
        base_url=f"https://{dc}.api.mailchimp.com/3.0",
        auth=("anystring", api_key),
        timeout=60,
    )


def _check(resp: httpx.Response, what: str) -> dict:
    if resp.status_code >= 400:
        raise SeedError(
            f"{what} failed: HTTP {resp.status_code} {resp.text[:500]}",
            status=resp.status_code,
        )
    return resp.json()


RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _call(what: str, send, attempts: int = 4) -> dict:
    """Issue a request via send(), retrying timeouts and transient statuses
    with exponential backoff. Anything else fails loudly via _check."""
    for attempt in range(1, attempts + 1):
        try:
            resp = send()
        except httpx.TransportError as exc:
            if attempt == attempts:
                raise SeedError(f"{what} failed after {attempts} attempts: {exc!r}") from exc
            time.sleep(2**attempt)
            continue
        if resp.status_code in RETRYABLE_STATUS and attempt < attempts:
            time.sleep(2**attempt)
            continue
        return _check(resp, what)
    raise SeedError(f"{what}: retry loop exhausted")  # pragma: no cover


def get_audience_id(client: httpx.Client) -> str:
    lists = _call("list audiences", lambda: client.get("/lists"))["lists"]
    if not lists:
        raise SeedError(
            "no audience found - create one in the Mailchimp UI first "
            "(the free tier ships with exactly one)"
        )
    return lists[0]["id"]


def ensure_merge_fields(client: httpx.Client, list_id: str) -> None:
    existing = _call(
        "list merge fields", lambda: client.get(f"/lists/{list_id}/merge-fields?count=50")
    )
    tags = {f["tag"] for f in existing["merge_fields"]}
    for field in MERGE_FIELDS:
        if field["tag"] not in tags:
            _call(
                f"create merge field {field['tag']}",
                lambda f=field: client.post(f"/lists/{list_id}/merge-fields", json=f),
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
    _call(
        f"upsert {sub.email}",
        lambda: client.put(f"/lists/{list_id}/members/{sub_hash}", json=body),
    )


def create_segments(client: httpx.Client, list_id: str) -> None:
    existing = _call("list segments", lambda: client.get(f"/lists/{list_id}/segments?count=50"))
    names = {s["name"] for s in existing["segments"]}
    for seg in SEGMENTS:
        if seg["name"] not in names:
            _call(
                f"segment {seg['name']}",
                lambda s=seg: client.post(f"/lists/{list_id}/segments", json=s),
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=450)
    parser.add_argument("--seed", type=int, default=542)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="generate and write ground_truth.json without touching Mailchimp",
    )
    parser.add_argument(
        "--start-at", type=int, default=0,
        help="skip the first N records (resume; upserts are idempotent)",
    )
    args = parser.parse_args()

    subs, truth = generate(count=args.count, seed=args.seed)
    if args.start_at == 0 or args.dry_run:
        # A resumed run does not own the manifest: it must not clobber a
        # ground truth already reconciled against observed rejections.
        GROUND_TRUTH_PATH.write_text(truth.model_dump_json(indent=2) + "\n")
        print(f"ground truth written to {GROUND_TRUTH_PATH}")
    print(json.dumps({k: v for k, v in truth.model_dump().items() if k != "suppressed_emails"}))

    if args.dry_run:
        return 0

    settings = Settings()
    if not settings.mailchimp_api_key:
        raise SeedError("ESP_MAILCHIMP_API_KEY is not set (see .env.example)")

    rejected: list[str] = []
    with client_for(settings.mailchimp_api_key) as client:
        list_id = get_audience_id(client)
        ensure_merge_fields(client, list_id)
        for i, sub in enumerate(subs[args.start_at:], args.start_at + 1):
            try:
                upsert_member(client, list_id, sub)
            except SeedError as exc:
                # Mailchimp's validity heuristic rejects some Faker addresses
                # ("looks fake or invalid"). Skip and reconcile ground truth
                # below; any other failure still fails loudly.
                if exc.status == 400 and "looks fake or invalid" in str(exc):
                    rejected.append(sub.email)
                    print(f"  rejected by Mailchimp, skipping: {sub.email}")
                else:
                    raise
            if i % 50 == 0:
                print(f"  {i}/{len(subs)} members upserted")
        reconcile(subs, rejected, args)
        create_segments(client, list_id)

    print(f"seeded {len(subs) - len(rejected)} members into audience {list_id}; "
          "now trigger the account export in the Mailchimp UI (see fixtures/README.md)")
    return 0


def reconcile(subs: list[Subscriber], rejected: list[str], args) -> None:
    """Rewrite ground truth over what actually landed; runs before segment
    creation so a late failure cannot lose the rejection record."""
    if rejected:
        # Ground truth must describe what actually landed in the account.
        kept = [s for s in subs if s.email not in set(rejected)]
        GROUND_TRUTH_PATH.write_text(
            _aggregate(kept, args.seed).model_dump_json(indent=2) + "\n"
        )
        REJECTED_PATH.write_text(json.dumps(rejected, indent=2) + "\n")
        print(
            f"{len(rejected)} addresses rejected by Mailchimp (see {REJECTED_PATH}); "
            f"ground truth reconciled to {len(kept)} seeded members"
        )
        if args.start_at:
            print(
                "NOTE: resumed run - rejections before --start-at were not observed; "
                "reconcile against the account totals before freezing the fixture"
            )


if __name__ == "__main__":
    sys.exit(main())
