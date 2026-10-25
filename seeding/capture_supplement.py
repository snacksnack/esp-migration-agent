"""Capture, via the API, source-account objects that Mailchimp's export
bundles omit or may omit: saved-segment DEFINITIONS (conditions), campaign
metadata + content, and template metadata. Written to fixtures/api_supplement/
as a documented part of the fixture (RC1-543 / ADR-0001) - mirroring real
engagements, where segment logic is pulled over the API during discovery.

Usage: uv run python -m seeding.capture_supplement
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

from seeding.mailchimp_seeder import Settings, _call, client_for, get_audience_id

OUT_DIR = Path(__file__).parent.parent / "fixtures" / "api_supplement"


def capture_segments(client: httpx.Client, list_id: str) -> list[dict]:
    data = _call("list segments", lambda: client.get(f"/lists/{list_id}/segments?count=50"))
    return [
        {k: s.get(k) for k in ("id", "name", "type", "options", "member_count", "created_at")}
        for s in data.get("segments", [])
    ]


def capture_templates(client: httpx.Client) -> list[dict]:
    data = _call("list templates", lambda: client.get("/templates?type=user&count=50"))
    return [
        {k: t.get(k) for k in ("id", "name", "type", "drag_and_drop", "date_created")}
        for t in data.get("templates", [])
    ]


def capture_campaigns(client: httpx.Client) -> list[dict]:
    data = _call("list campaigns", lambda: client.get("/campaigns?count=50"))
    campaigns = []
    for c in data.get("campaigns", []):
        content = _call(
            f"campaign content {c['id']}",
            lambda cid=c["id"]: client.get(f"/campaigns/{cid}/content?fields=html,plain_text"),
        )
        settings = dict(c.get("settings") or {})
        if settings.get("reply_to"):
            # Account-owner sender identity, masked so the committed fixture
            # stays 100% synthetic (fixtures/README.md).
            settings["reply_to"] = "owner@redacted.invalid"
        campaigns.append(
            {
                "id": c["id"],
                "status": c.get("status"),
                "type": c.get("type"),
                "settings": settings,
                "recipients": c.get("recipients"),
                "content": content,
            }
        )
    return campaigns


def main() -> int:
    settings = Settings()
    if not settings.mailchimp_api_key:
        raise SystemExit("ESP_MAILCHIMP_API_KEY is not set (see .env.example)")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with client_for(settings.mailchimp_api_key) as client:
        list_id = get_audience_id(client)
        for name, payload in (
            ("segments", capture_segments(client, list_id)),
            ("templates", capture_templates(client)),
            ("campaigns", capture_campaigns(client)),
        ):
            path = OUT_DIR / f"{name}.json"
            path.write_text(json.dumps(payload, indent=2) + "\n")
            print(f"{path}: {len(payload)} objects")
    return 0


if __name__ == "__main__":
    sys.exit(main())
