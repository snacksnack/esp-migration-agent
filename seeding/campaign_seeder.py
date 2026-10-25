"""Seed templates and DRAFT campaigns into the Mailchimp account (RC1-543).

Gives the discovery stage campaign metadata and merge-tag-rich template HTML
to inventory, and the mapping stage real *|TAG|* material to translate.
Drafts only: nothing is ever sent - a send to thousands of synthetic
addresses would mass-bounce and get the account suspended.

Usage: uv run python -m seeding.campaign_seeder
"""

from __future__ import annotations

import sys

import httpx

from seeding.mailchimp_seeder import (
    Settings,
    _call,
    client_for,
    get_audience_id,
)

NEWSLETTER_HTML = """<!doctype html>
<html><body>
<h1>The Monthly Dispatch</h1>
<p>Hi *|FNAME|*,</p>
<p>Here is what's new this month for our readers in *|STATE|*.</p>
<p>Your last purchase was on *|LASTPURCH|* - we picked a few things
you might like since then.</p>
<p>- The Dispatch team</p>
<hr>
<p><a href="*|UNSUB|*">Unsubscribe</a> | <a href="*|ARCHIVE|*">View in browser</a></p>
</body></html>
"""

PROMO_HTML = """<!doctype html>
<html><body>
<h1>A deal just for you, *|FNAME|* *|LNAME|*</h1>
<p>Shoppers near *|ZIP|* get free delivery this week.</p>
<p><a href="https://shop.example.test/deals">Shop the sale</a></p>
<hr>
<p><a href="*|UNSUB|*">Unsubscribe</a></p>
</body></html>
"""

TEMPLATES: list[tuple[str, str]] = [
    ("Monthly newsletter", NEWSLETTER_HTML),
    ("Promo blast", PROMO_HTML),
]

# title, subject, template name, saved-segment name (None = whole audience)
CAMPAIGNS: list[tuple[str, str, str, str | None]] = [
    ("October newsletter", "Your October update", "Monthly newsletter", None),
    ("Brooklyn free delivery", "Free delivery in your area", "Promo blast", "Brooklyn subscribers"),
    ("VIP re-engagement", "We miss you", "Promo blast", "Highly engaged (4-5 stars)"),
]


def ensure_templates(client: httpx.Client) -> dict[str, int]:
    existing = _call("list templates", lambda: client.get("/templates?type=user&count=50"))
    ids = {t["name"]: t["id"] for t in existing.get("templates", [])}
    for name, html in TEMPLATES:
        if name not in ids:
            created = _call(
                f"create template {name}",
                lambda n=name, h=html: client.post("/templates", json={"name": n, "html": h}),
            )
            ids[name] = created["id"]
    return ids


def segment_ids(client: httpx.Client, list_id: str) -> dict[str, int]:
    existing = _call("list segments", lambda: client.get(f"/lists/{list_id}/segments?count=50"))
    return {s["name"]: s["id"] for s in existing.get("segments", [])}


def ensure_campaigns(
    client: httpx.Client, list_id: str, template_ids: dict[str, int], reply_to: str
) -> None:
    existing = _call("list campaigns", lambda: client.get("/campaigns?count=50"))
    titles = {c["settings"].get("title") for c in existing.get("campaigns", [])}
    segments = segment_ids(client, list_id)
    for title, subject, template_name, segment_name in CAMPAIGNS:
        if title in titles:
            continue
        recipients: dict = {"list_id": list_id}
        if segment_name:
            recipients["segment_opts"] = {"saved_segment_id": segments[segment_name]}
        body = {
            "type": "regular",
            "recipients": recipients,
            "settings": {
                "title": title,
                "subject_line": subject,
                "from_name": "The Dispatch Shop",
                "reply_to": reply_to,
            },
        }
        campaign = _call(
            f"create campaign {title}", lambda b=body: client.post("/campaigns", json=b)
        )
        _call(
            f"set content {title}",
            lambda cid=campaign["id"], tid=template_ids[template_name]: client.put(
                f"/campaigns/{cid}/content", json={"template": {"id": tid}}
            ),
        )
        print(f"draft campaign created: {title}")


def main() -> int:
    settings = Settings()
    if not settings.mailchimp_api_key:
        raise SystemExit("ESP_MAILCHIMP_API_KEY is not set (see .env.example)")
    with client_for(settings.mailchimp_api_key) as client:
        account = _call("account root", lambda: client.get("/?fields=email"))
        list_id = get_audience_id(client)
        template_ids = ensure_templates(client)
        ensure_campaigns(client, list_id, template_ids, reply_to=account["email"])
    print("templates and draft campaigns in place; nothing was or will be sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
