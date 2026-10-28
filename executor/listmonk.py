"""Listmonk API adapter (RC1-546): the dev target (ADR-0002).

Implements executor.target.TargetClient over Listmonk's REST API. Dialect
notes live here, not in the plan: how consent states express as (status,
subscription_status), how per-list subscription lands (preconfirm on create),
and how segment queries are counted (the subscribers query endpoint).

Auth is HTTP basic with an API user and token (Listmonk v3+; the admin
credentials created at --install also work). Settings are env-backed with the
ESP_ prefix; everything optional, nothing here is imported into a test's
network path.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from executor.plan import DesiredSubscriber, PlannedCampaign
from executor.retries import IDEMPOTENT_METHODS, Recovered, call_with_retries
from executor.target import TargetError, TargetSubscriber

PAGE_SIZE = 1000
LOOKUP_BATCH = 200  # addresses per IN (...) lookup query


class ListmonkSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ESP_", env_file=".env", extra="ignore")

    listmonk_url: str | None = None
    listmonk_username: str | None = None
    listmonk_password: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.listmonk_url and self.listmonk_username and self.listmonk_password)


def client_from_settings(settings: ListmonkSettings) -> ListmonkClient:
    if not settings.configured:
        raise TargetError(
            "Listmonk is not configured: set ESP_LISTMONK_URL, "
            "ESP_LISTMONK_USERNAME, ESP_LISTMONK_PASSWORD (see .env.example)"
        )
    return ListmonkClient(settings.listmonk_url, settings.listmonk_username,
                          settings.listmonk_password)


class ListmonkClient:
    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        # transport/sleep are injection points for the no-network failure
        # matrix (RC1-570): httpx.MockTransport in, recorded backoffs out.
        self._http = httpx.Client(
            base_url=url.rstrip("/") + "/api",
            auth=(username, password),
            timeout=60,
            transport=transport,
        )
        self._sleep = sleep
        self._api_user = username
        self.recovered_calls = 0  # read-backs that stood in for a lost response

    def _call(
        self,
        method: str,
        path: str,
        what: str,
        recover: Callable[[], dict | None] | None = None,
        **kwargs,
    ) -> dict | list:
        result = call_with_retries(
            lambda: self._http.request(method, path, **kwargs),
            what=what,
            replay_safe=method in IDEMPOTENT_METHODS,
            recover=recover,
            sleep=self._sleep,
        )
        if isinstance(result, Recovered):
            self.recovered_calls += 1
            return result.data
        if result.status_code >= 400:
            raise TargetError(f"{what}: HTTP {result.status_code} {result.text[:300]}")
        return result.json().get("data")

    def describe_destination(self) -> dict[str, str]:
        """Which Listmonk this is, for the execution journal (RC1-571)."""
        return {
            "platform": "listmonk",
            "host": str(self._http.base_url).rstrip("/"),
            "api_user": self._api_user,
        }

    # --- lists ---------------------------------------------------------------

    def find_list(self, name: str) -> int | None:
        data = self._call("GET", "/lists", "list lists", params={"per_page": "all"})
        rows = data["results"] if isinstance(data, dict) else data
        for row in rows:
            if row["name"] == name:
                return row["id"]
        return None

    def _recover_list(self, name: str) -> dict | None:
        found = self.find_list(name)
        return None if found is None else {"id": found}

    def ensure_list(self, name: str) -> int:
        found = self.find_list(name)
        if found is not None:
            return found
        created = self._call(
            "POST",
            "/lists",
            f"create list {name!r}",
            recover=lambda: self._recover_list(name),
            json={"name": name, "type": "private", "optin": "double"},
        )
        return created["id"]

    # --- subscribers ---------------------------------------------------------

    @staticmethod
    def _normalize(row: dict, list_id: int | None) -> TargetSubscriber:
        lists = row.get("lists") or []
        subscription = next(
            (
                lst.get("subscription_status", "")
                for lst in lists
                if lst.get("id") == list_id
            ),
            "",
        )
        return TargetSubscriber(
            id=row["id"],
            email=row["email"],
            name=row.get("name") or "",
            status=row["status"],
            subscription=subscription,
            attribs=row.get("attribs") or {},
            list_ids=sorted(lst["id"] for lst in lists if "id" in lst),
        )

    def lookup_subscribers(self, emails: list[str]) -> dict[str, TargetSubscriber]:
        out: dict[str, TargetSubscriber] = {}
        for start in range(0, len(emails), LOOKUP_BATCH):
            batch = emails[start:start + LOOKUP_BATCH]
            quoted = ", ".join("'" + e.lower().replace("'", "''") + "'" for e in batch)
            data = self._call(
                "GET",
                "/subscribers",
                f"look up {len(batch)} subscriber(s)",
                params={
                    "query": f"LOWER(subscribers.email) IN ({quoted})",
                    "page": 1,
                    "per_page": len(batch),
                },
            )
            for row in data["results"]:
                sub = self._normalize(row, None)
                out[sub.email.lower()] = sub
        return out

    def attach_subscriber(self, existing: TargetSubscriber, list_id: int) -> None:
        # Bulk add with subscription "unsubscribed": membership only. The
        # global status (blocklisted) is untouched - probed against a live
        # Listmonk (ADR-0018).
        self._call(
            "PUT",
            "/subscribers/lists",
            f"attach {existing.email}",
            json={
                "ids": [existing.id],
                "action": "add",
                "target_list_ids": [list_id],
                "status": "unsubscribed",
            },
        )

    def current_subscribers(self, list_id: int) -> dict[str, TargetSubscriber]:
        out: dict[str, TargetSubscriber] = {}
        page = 1
        while True:
            data = self._call(
                "GET",
                "/subscribers",
                "list subscribers",
                params={"list_id": list_id, "page": page, "per_page": PAGE_SIZE},
            )
            for row in data["results"]:
                sub = self._normalize(row, list_id)
                out[sub.email.lower()] = sub
            if page * PAGE_SIZE >= data["total"]:
                return out
            page += 1

    def _subscriber_body(
        self, desired: DesiredSubscriber, list_id: int, keep_lists: list[int] | None = None
    ) -> dict:
        return {
            "email": desired.email,
            "name": desired.name,
            "status": desired.status,
            # PUT REPLACES memberships: sending only this list would drop the
            # record from every other list it is on. Existing subscriptions
            # keep their status when re-listed (probed, ADR-0018).
            "lists": sorted({*(keep_lists or []), list_id}),
            "attribs": desired.attribs,
            # confirmed lands via preconfirm; unconfirmed is the double-optin
            # default. No opt-in email is triggered by the import itself.
            "preconfirm_subscriptions": desired.subscription == "confirmed",
        }

    def _recover_subscriber(self, email: str) -> dict | None:
        """Read-back for an ambiguous create: did the subscriber land?"""
        quoted = email.replace("'", "''")
        data = self._call(
            "GET",
            "/subscribers",
            f"look up subscriber {email}",
            params={
                "query": f"subscribers.email = '{quoted}'",
                "page": 1,
                "per_page": 1,
            },
        )
        rows = data["results"]
        return rows[0] if rows else None

    def create_subscriber(self, desired: DesiredSubscriber, list_id: int) -> None:
        created = self._call(
            "POST",
            "/subscribers",
            f"create subscriber {desired.email}",
            recover=lambda: self._recover_subscriber(desired.email),
            json=self._subscriber_body(desired, list_id),
        )
        if desired.subscription == "unsubscribed" and desired.status != "blocklisted":
            self._set_subscription(created["id"], list_id, "unsubscribe", desired.email)

    def update_subscriber(
        self, existing: TargetSubscriber, desired: DesiredSubscriber, list_id: int
    ) -> None:
        self._call(
            "PUT",
            f"/subscribers/{existing.id}",
            f"update subscriber {desired.email}",
            json=self._subscriber_body(desired, list_id, keep_lists=existing.list_ids),
        )
        if desired.subscription == "unsubscribed" and desired.status != "blocklisted":
            self._set_subscription(existing.id, list_id, "unsubscribe", desired.email)

    def _set_subscription(self, sub_id: int, list_id: int, action: str, email: str) -> None:
        self._call(
            "PUT",
            "/subscribers/lists",
            f"{action} {email}",
            json={"ids": [sub_id], "action": action, "target_list_ids": [list_id]},
        )

    def count_query(self, list_id: int, query: str) -> int:
        data = self._call(
            "GET",
            "/subscribers",
            f"count query {query!r}",
            params={"list_id": list_id, "query": query, "page": 1, "per_page": 1},
        )
        return data["total"]

    # --- campaigns -----------------------------------------------------------

    def current_campaigns(self) -> dict[str, PlannedCampaign]:
        data = self._call(
            "GET", "/campaigns", "list campaigns", params={"per_page": "all"}
        )
        out: dict[str, PlannedCampaign] = {}
        for row in data["results"]:
            body = self._call(
                "GET", f"/campaigns/{row['id']}", f"campaign {row['name']!r}"
            )
            out[row["name"]] = PlannedCampaign(
                name=row["name"],
                subject=row.get("subject") or "",
                body=body.get("body") or "",
            )
        return out

    def _find_campaign_row(self, name: str) -> dict | None:
        data = self._call("GET", "/campaigns", "list campaigns", params={"per_page": "all"})
        return next((r for r in data["results"] if r["name"] == name), None)

    def create_campaign(self, campaign: PlannedCampaign, list_id: int) -> None:
        self._call(
            "POST",
            "/campaigns",
            f"create campaign {campaign.name!r}",
            recover=lambda: self._find_campaign_row(campaign.name),
            json={
                "name": campaign.name,
                "subject": campaign.subject,
                "lists": [list_id],
                "type": "regular",
                "content_type": "html",
                "body": campaign.body,
            },
        )

    def update_campaign(self, name: str, campaign: PlannedCampaign, list_id: int) -> None:
        row = self._find_campaign_row(name)
        if row is None:
            raise TargetError(f"campaign {name!r} vanished between diff and write")
        self._call(
            "PUT",
            f"/campaigns/{row['id']}",
            f"update campaign {name!r}",
            json={
                "name": campaign.name,
                "subject": campaign.subject,
                "lists": [list_id],
                "type": "regular",
                "content_type": "html",
                "body": campaign.body,
            },
        )
