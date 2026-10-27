"""RC1-570 failure matrix, Brevo adapter: rate limits, expired keys, pagination
failures mid-fetch, and ambiguous write outcomes. The contact upsert replays
(updateEnabled), campaign creation reads back - the two POST postures differ on
purpose. httpx.MockTransport only, no network, no real sleeps."""

from __future__ import annotations

import httpx
import pytest

from executor.brevo import PAGE_SIZE, BrevoClient
from executor.plan import DesiredSubscriber, PlannedCampaign
from executor.retries import MAX_ATTEMPTS
from executor.target import TargetError


def client_with(handler, sleeps: list[float]) -> BrevoClient:
    return BrevoClient(
        "test-key", transport=httpx.MockTransport(handler), sleep=sleeps.append
    )


def contact_row(contact_id: int, email: str) -> dict:
    return {
        "id": contact_id,
        "email": email,
        "emailBlacklisted": False,
        "attributes": {"ESP_CONSENT": "enabled/confirmed"},
    }


def desired(email: str) -> DesiredSubscriber:
    return DesiredSubscriber(
        email=email,
        name="Sub",
        status="enabled",
        subscription="confirmed",
        subscribable=True,
        suppressed=False,
        attribs={},
    )


# --- rate limits -----------------------------------------------------------------


def test_rate_limited_contacts_page_honors_retry_after():
    state = {"gets": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["gets"] += 1
        if state["gets"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(
            200, json={"contacts": [contact_row(1, "a@example.com")], "count": 1}
        )

    sleeps: list[float] = []
    out = client_with(handler, sleeps).current_subscribers(1)
    assert set(out) == {"a@example.com"}
    assert sleeps == [2.0]


# --- expired credentials mid-run ----------------------------------------------------


def test_expired_key_fails_loudly_without_retry():
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        return httpx.Response(401, json={"message": "API key expired", "code": "unauthorized"})

    sleeps: list[float] = []
    with pytest.raises(TargetError, match="HTTP 401"):
        client_with(handler, sleeps).current_subscribers(1)
    assert state["calls"] == 1
    assert sleeps == []


# --- pagination failures partway through current_subscribers ------------------------


def test_gateway_failure_mid_pagination_completes_the_fetch():
    state = {"page2": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        if offset == 0:
            rows = [contact_row(i, f"u{i}@example.com") for i in range(PAGE_SIZE)]
            return httpx.Response(200, json={"contacts": rows, "count": PAGE_SIZE + 1})
        state["page2"] += 1
        if state["page2"] == 1:
            return httpx.Response(502)  # a read is replay-safe: retried
        return httpx.Response(
            200,
            json={"contacts": [contact_row(PAGE_SIZE, "last@example.com")],
                  "count": PAGE_SIZE + 1},
        )

    sleeps: list[float] = []
    out = client_with(handler, sleeps).current_subscribers(1)
    assert len(out) == PAGE_SIZE + 1
    assert "last@example.com" in out


def test_persistent_failure_mid_pagination_raises_never_partial():
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        if offset == 0:
            rows = [contact_row(i, f"u{i}@example.com") for i in range(PAGE_SIZE)]
            return httpx.Response(200, json={"contacts": rows, "count": PAGE_SIZE + 1})
        return httpx.Response(503)

    sleeps: list[float] = []
    with pytest.raises(TargetError, match=f"HTTP 503 after {MAX_ATTEMPTS} attempts"):
        client_with(handler, sleeps).current_subscribers(1)


# --- ambiguous outcomes --------------------------------------------------------------


def test_contact_upsert_replays_after_timeout():
    """POST /contacts carries updateEnabled, so a replay after an ambiguous
    failure is an upsert, not a duplicate: no read-back needed."""
    state = {"posts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/contacts/attributes"):
            return httpx.Response(200, json={"attributes": [{"name": "ESP_CONSENT"}]})
        assert request.method == "POST" and request.url.path.endswith("/contacts")
        state["posts"] += 1
        if state["posts"] == 1:
            raise httpx.ReadTimeout("read timed out")
        return httpx.Response(201, json={"id": 1})

    sleeps: list[float] = []
    client_with(handler, sleeps).create_subscriber(desired("a@example.com"), 1)
    assert state["posts"] == 2


def test_campaign_create_timeout_reads_back_instead_of_duplicating():
    """Brevo allows duplicate campaign names, so a blind replay WOULD
    duplicate: an ambiguous create must find the landed campaign by name."""
    state = {"posts": 0, "lists": 0}
    campaign = PlannedCampaign(name="Welcome", subject="Hi", body="<p>Hi</p>")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/senders"):
            return httpx.Response(
                200, json={"senders": [{"name": "R", "email": "r@example.com"}]}
            )
        if request.method == "POST":
            state["posts"] += 1
            raise httpx.ReadTimeout("read timed out")  # ...but the draft landed
        state["lists"] += 1
        return httpx.Response(
            200, json={"campaigns": [{"id": 9, "name": "Welcome"}], "count": 1}
        )

    sleeps: list[float] = []
    client_with(handler, sleeps).create_campaign(campaign, 1)
    assert state["posts"] == 1  # never replayed
    assert state["lists"] == 1  # the read-back confirmed it landed
