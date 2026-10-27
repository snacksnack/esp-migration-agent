"""Brevo API adapter (RC1-547): the live third-party target (ADR-0002).

Implements the same TargetClient protocol as the Listmonk adapter - the
executor core does not change. The approved spec stays canonical; every
platform difference is a translation THIS module owns (ADR-0007):

- Attribute names: canonical lowercase keys <-> Brevo's global UPPERCASE
  attributes (first_name <-> FIRST_NAME). Reversed on read.
- Merge tags: the plan's canonical template syntax <-> Brevo's
  {{ contact.X }} / {{ unsubscribe }} / {{ mirror }}. A bijection, applied on
  write and reversed on read, so the engine's convergence and render checks
  compare in one vocabulary.
- Consent: Brevo has no per-list subscription states, only a global
  emailBlacklisted flag. The canonical label is persisted in an ESP_CONSENT
  attribute and the flag is derived from its subscribability. On read, the
  label is trusted ONLY while the actual flag still agrees with it; a contact
  whose blacklist flag was flipped behind the executor's back is reported by
  its ACTUAL mailability, so reconciliation (suppression integrity above all)
  fails loudly instead of trusting a stale label.
- Segments: Brevo has no server-side query API; count_query evaluates the
  spec's query dialect client-side over the fetched contacts
  (executor/queries.py), same as reconciliation does plan-side.

Demo-safety: this adapter never sends - campaigns are created as drafts and
suppressed contacts land emailBlacklisted. Needs ESP_BREVO_API_KEY.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from executor.plan import DesiredSubscriber, PlannedCampaign
from executor.queries import evaluate_query
from executor.retries import IDEMPOTENT_METHODS, Recovered, call_with_retries
from executor.target import TargetError, TargetSubscriber

BREVO_API = "https://api.brevo.com/v3"
PAGE_SIZE = 500  # Brevo's max `limit` on the list-contacts endpoint
LOOKUP_PAGE_SIZE = 50  # Brevo's max `limit` on the folders/lists endpoints
CONSENT_ATTR = "ESP_CONSENT"
FOLDER_NAME = "ESP migration"

# Canonical (status, subscription) per consent label - must mirror
# executor.plan.CONSENT_DIALECTS; the adapter maps labels, never invents them.
CANONICAL_STATES = {
    "enabled/confirmed": ("enabled", "confirmed"),
    "enabled/unconfirmed": ("enabled", "unconfirmed"),
    "unsubscribed": ("enabled", "unsubscribed"),
    "blocklisted": ("blocklisted", "unsubscribed"),
}

# How each canonical label lands on Brevo's one consent control. Brevo cannot
# represent "unconfirmed" (double opt-in is form-driven, not importable), so
# the safe landing for unconfirmed contacts is NOT MAILABLE: they are
# retained with their ESP_CONSENT label but blacklisted until the customer
# runs a re-permission pass. Erring mailable would manufacture consent.
BREVO_BLACKLISTED: dict[str, bool] = {
    "enabled/confirmed": False,
    "enabled/unconfirmed": True,
    "unsubscribed": True,
    "blocklisted": True,
}

# Canonical template expressions <-> Brevo template language. A bijection:
# write-translate with TAG_TO_BREVO, read-translate back with the inverse.
TAG_TO_BREVO = {
    "{{ .Subscriber.Attribs.first_name }}": "{{ contact.FIRST_NAME }}",
    "{{ .Subscriber.Attribs.last_name }}": "{{ contact.LAST_NAME }}",
    "{{ .Subscriber.Attribs.state }}": "{{ contact.STATE }}",
    "{{ .Subscriber.Attribs.zip }}": "{{ contact.ZIP }}",
    "{{ .Subscriber.Attribs.last_purchase }}": "{{ contact.LAST_PURCHASE }}",
    "{{ .Subscriber.Email }}": "{{ contact.EMAIL }}",
    "{{ UnsubscribeURL }}": "{{ unsubscribe }}",
    "{{ MessageURL }}": "{{ mirror }}",
}
TAG_FROM_BREVO = {v: k for k, v in TAG_TO_BREVO.items()}


class BrevoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ESP_", env_file=".env", extra="ignore")

    brevo_api_key: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.brevo_api_key)


def client_from_settings(settings: BrevoSettings) -> BrevoClient:
    if not settings.configured:
        raise TargetError("Brevo is not configured: set ESP_BREVO_API_KEY (see .env.example)")
    return BrevoClient(settings.brevo_api_key)


def attrib_to_brevo(key: str) -> str:
    return key.upper()


def attrib_from_brevo(name: str) -> str:
    return name.lower()


def _translate(body: str, mapping: dict[str, str]) -> str:
    out = body
    for src, dst in mapping.items():
        out = out.replace(src, dst)
    return out


def body_to_brevo(body: str) -> str:
    translated = _translate(body, TAG_TO_BREVO)
    residual = re.findall(r"\{\{\s*\.Subscriber[^}]*\}\}|\{\{\s*\w+URL\s*\}\}", translated)
    if residual:
        raise TargetError(
            f"campaign body carries canonical tag(s) {sorted(set(residual))} with no "
            "Brevo translation; extend TAG_TO_BREVO rather than migrating them literally"
        )
    return translated


def body_from_brevo(body: str) -> str:
    return _translate(body, TAG_FROM_BREVO)


def normalize_value(value):
    """Brevo returns number attributes as floats; integral floats come back
    as the ints the plan wrote. Everything else passes through."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def consent_to_brevo(desired: DesiredSubscriber) -> tuple[str, bool]:
    """(ESP_CONSENT label, emailBlacklisted). The label preserves the
    fine-grained state Brevo cannot; the flag lands per BREVO_BLACKLISTED."""
    label = next(
        (
            lbl
            for lbl, state in CANONICAL_STATES.items()
            if state == (desired.status, desired.subscription)
        ),
        None,
    )
    if label is None:
        raise TargetError(
            f"no canonical consent label for {desired.status}/{desired.subscription}"
        )
    return label, BREVO_BLACKLISTED[label]


def consent_from_brevo(label: str | None, blacklisted: bool) -> tuple[str, str]:
    """(status, subscription) for the engine. The stored label is trusted ONLY
    while the actual blacklist flag is where that label lands it; on
    disagreement the ACTUAL mailability wins, so a contact un-blacklisted
    behind the executor's back fails counts and suppression integrity."""
    if label in CANONICAL_STATES and BREVO_BLACKLISTED[label] == blacklisted:
        return CANONICAL_STATES[label]
    return ("blocklisted", "unsubscribed") if blacklisted else ("enabled", "confirmed")


class BrevoClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        # transport/sleep are injection points for the no-network failure
        # matrix (RC1-570): httpx.MockTransport in, recorded backoffs out.
        self._http = httpx.Client(
            base_url=BREVO_API,
            headers={"api-key": api_key, "accept": "application/json"},
            timeout=60,
            transport=transport,
        )
        self._sleep = sleep
        self._defined_attributes: set[str] | None = None

    def _call(
        self,
        method: str,
        path: str,
        what: str,
        ok_codes: set[int] | None = None,
        replay_safe: bool | None = None,
        recover: Callable[[], dict | None] | None = None,
        **kwargs,
    ) -> dict:
        if replay_safe is None:
            replay_safe = method in IDEMPOTENT_METHODS
        result = call_with_retries(
            lambda: self._http.request(method, path, **kwargs),
            what=what,
            replay_safe=replay_safe,
            recover=recover,
            sleep=self._sleep,
        )
        if isinstance(result, Recovered):
            return result.data
        resp = result
        if resp.status_code >= 400 and resp.status_code not in (ok_codes or set()):
            raise TargetError(f"{what}: HTTP {resp.status_code} {resp.text[:300]}")
        return resp.json() if resp.content else {}

    # --- lists -----------------------------------------------------------------

    def _lookup_pages(self, path: str, what: str, key: str):
        """Every page of a folders/lists lookup (their max `limit` is 50;
        a single first-page fetch would miss a 51st entry and create a
        duplicate, RC1-564)."""
        offset = 0
        while True:
            data = self._call(
                "GET", path, what,
                params={"limit": LOOKUP_PAGE_SIZE, "offset": offset},
            )
            rows = data.get(key) or []
            yield from rows
            offset += LOOKUP_PAGE_SIZE
            if offset >= data.get("count", 0) or not rows:
                return

    def _find_folder(self) -> int | None:
        for row in self._lookup_pages("/contacts/folders", "list folders", "folders"):
            if row["name"] == FOLDER_NAME:
                return row["id"]
        return None

    def _recover_folder(self) -> dict | None:
        found = self._find_folder()
        return None if found is None else {"id": found}

    def _ensure_folder(self) -> int:
        found = self._find_folder()
        if found is not None:
            return found
        created = self._call("POST", "/contacts/folders", "create folder",
                             recover=self._recover_folder,
                             json={"name": FOLDER_NAME})
        return created["id"]

    def find_list(self, name: str) -> int | None:
        for row in self._lookup_pages("/contacts/lists", "list lists", "lists"):
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
            "/contacts/lists",
            f"create list {name!r}",
            recover=lambda: self._recover_list(name),
            json={"name": name, "folderId": self._ensure_folder()},
        )
        return created["id"]

    # --- attributes ------------------------------------------------------------

    def _ensure_attributes(self, attribs: dict[str, str | int]) -> None:
        if self._defined_attributes is None:
            data = self._call("GET", "/contacts/attributes", "list attributes")
            self._defined_attributes = {
                a["name"] for a in data.get("attributes") or []
            }
        wanted = {attrib_to_brevo(k): v for k, v in attribs.items()}
        wanted[CONSENT_ATTR] = "text"
        for name, sample in wanted.items():
            if name in self._defined_attributes:
                continue
            attr_type = "float" if isinstance(sample, int) else "text"
            # 409 = already exists (raced or pre-existing): fine either way.
            # That tolerance also makes a replay after an ambiguous failure
            # safe: a landed define answers the retry with an accepted 409.
            self._call(
                "POST",
                f"/contacts/attributes/normal/{name}",
                f"define attribute {name}",
                ok_codes={409},
                replay_safe=True,
                json={"type": attr_type},
            )
            self._defined_attributes.add(name)

    # --- subscribers -----------------------------------------------------------

    def _normalize(self, row: dict) -> TargetSubscriber:
        raw_attrs = {
            attrib_from_brevo(k): normalize_value(v)
            for k, v in (row.get("attributes") or {}).items()
        }
        label = raw_attrs.pop(CONSENT_ATTR.lower(), None)
        first = str(raw_attrs.get("first_name", "") or "")
        last = str(raw_attrs.get("last_name", "") or "")
        status, subscription = consent_from_brevo(label, bool(row.get("emailBlacklisted")))
        return TargetSubscriber(
            id=row["id"],
            email=row["email"],
            name=f"{first} {last}".strip(),
            status=status,
            subscription=subscription,
            attribs=raw_attrs,
        )

    def current_subscribers(self, list_id: int) -> dict[str, TargetSubscriber]:
        out: dict[str, TargetSubscriber] = {}
        offset = 0
        while True:
            data = self._call(
                "GET",
                f"/contacts/lists/{list_id}/contacts",
                "list contacts",
                params={"limit": PAGE_SIZE, "offset": offset},
            )
            rows = data.get("contacts") or []
            for row in rows:
                sub = self._normalize(row)
                out[sub.email.lower()] = sub
            offset += PAGE_SIZE
            if offset >= data.get("count", 0) or not rows:
                return out

    def _contact_body(self, desired: DesiredSubscriber, list_id: int) -> dict:
        label, blacklisted = consent_to_brevo(desired)
        attributes = {attrib_to_brevo(k): v for k, v in desired.attribs.items()}
        attributes[CONSENT_ATTR] = label
        return {
            "email": desired.email,
            "attributes": attributes,
            "listIds": [list_id],
            "emailBlacklisted": blacklisted,
            "updateEnabled": True,  # upsert semantics on Brevo's side too
        }

    def create_subscriber(self, desired: DesiredSubscriber, list_id: int) -> None:
        self._ensure_attributes(desired.attribs)
        self._call(
            "POST",
            "/contacts",
            f"create contact {desired.email}",
            ok_codes={204},  # updateEnabled returns 204 when it updated instead
            replay_safe=True,  # updateEnabled makes this POST an upsert
            json=self._contact_body(desired, list_id),
        )

    def update_subscriber(
        self, existing: TargetSubscriber, desired: DesiredSubscriber, list_id: int
    ) -> None:
        self._ensure_attributes(desired.attribs)
        body = self._contact_body(desired, list_id)
        self._call(
            "PUT",
            f"/contacts/{existing.id}",
            f"update contact {desired.email}",
            ok_codes={204},
            json={k: v for k, v in body.items() if k != "updateEnabled"},
        )

    def count_query(self, list_id: int, query: str) -> int:
        current = self.current_subscribers(list_id)
        return sum(1 for s in current.values() if evaluate_query(query, s.attribs))

    # --- campaigns (drafts only; nothing sends) --------------------------------

    def _campaign_rows(self) -> list[dict]:
        data = self._call("GET", "/emailCampaigns", "list campaigns",
                          params={"limit": 100, "offset": 0})
        return data.get("campaigns") or []

    def current_campaigns(self) -> dict[str, PlannedCampaign]:
        out: dict[str, PlannedCampaign] = {}
        for row in self._campaign_rows():
            detail = self._call(
                "GET", f"/emailCampaigns/{row['id']}", f"campaign {row['name']!r}"
            )
            out[row["name"]] = PlannedCampaign(
                name=row["name"],
                subject=detail.get("subject") or "",
                body=body_from_brevo(detail.get("htmlContent") or ""),
            )
        return out

    def _sender(self) -> dict:
        data = self._call("GET", "/senders", "list senders")
        senders = data.get("senders") or []
        if not senders:
            raise TargetError("Brevo account has no sender; add one in the Brevo UI")
        return {"name": senders[0]["name"], "email": senders[0]["email"]}

    def _campaign_body(self, campaign: PlannedCampaign, list_id: int) -> dict:
        return {
            "name": campaign.name,
            "subject": campaign.subject,
            "sender": self._sender(),
            "htmlContent": body_to_brevo(campaign.body),
            "recipients": {"listIds": [list_id]},
        }

    def _find_campaign_row(self, name: str) -> dict | None:
        return next((r for r in self._campaign_rows() if r["name"] == name), None)

    def create_campaign(self, campaign: PlannedCampaign, list_id: int) -> None:
        self._call(
            "POST",
            "/emailCampaigns",
            f"create campaign {campaign.name!r}",
            recover=lambda: self._find_campaign_row(campaign.name),
            json=self._campaign_body(campaign, list_id),
        )

    def update_campaign(self, name: str, campaign: PlannedCampaign, list_id: int) -> None:
        row = self._find_campaign_row(name)
        if row is None:
            raise TargetError(f"campaign {name!r} vanished between diff and write")
        self._call(
            "PUT",
            f"/emailCampaigns/{row['id']}",
            f"update campaign {name!r}",
            ok_codes={204},
            json=self._campaign_body(campaign, list_id),
        )
