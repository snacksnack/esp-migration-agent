"""RC1-570 failure matrix, Listmonk adapter: rate limits, expired credentials,
pagination failures mid-fetch, and ambiguous write outcomes - all scripted
through httpx.MockTransport, no network, no real sleeps."""

from __future__ import annotations

import httpx
import pytest

from executor.listmonk import PAGE_SIZE, ListmonkClient
from executor.plan import DesiredSubscriber
from executor.retries import MAX_ATTEMPTS
from executor.target import TargetError


def client_with(handler, sleeps: list[float]) -> ListmonkClient:
    return ListmonkClient(
        "http://listmonk.test",
        "api-user",
        "token",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )


def data(payload) -> httpx.Response:
    return httpx.Response(200, json={"data": payload})


def sub_row(sub_id: int, email: str, subscription: str = "confirmed") -> dict:
    return {
        "id": sub_id,
        "email": email,
        "name": "Sub",
        "status": "enabled",
        "lists": [{"id": 1, "subscription_status": subscription}],
        "attribs": {},
    }


def desired(email: str, subscription: str = "confirmed") -> DesiredSubscriber:
    return DesiredSubscriber(
        email=email,
        name="Sub",
        status="enabled",
        subscription=subscription,
        subscribable=subscription in ("confirmed", "unconfirmed"),
        suppressed=False,
        attribs={},
    )


# --- rate limits -----------------------------------------------------------------


def test_rate_limited_page_is_retried_after_the_header():
    state = {"gets": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["gets"] += 1
        if state["gets"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return data({"results": [sub_row(1, "a@example.com")], "total": 1})

    sleeps: list[float] = []
    out = client_with(handler, sleeps).current_subscribers(1)
    assert set(out) == {"a@example.com"}
    assert state["gets"] == 2
    assert sleeps == [3.0]


# --- expired credentials mid-run ---------------------------------------------------


def test_expired_credentials_fail_loudly_without_retry():
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:  # page 1 succeeds, then the session expires
            return data({"results": [sub_row(1, "a@example.com")], "total": PAGE_SIZE + 1})
        return httpx.Response(401, text="invalid session")

    sleeps: list[float] = []
    with pytest.raises(TargetError, match="HTTP 401"):
        client_with(handler, sleeps).current_subscribers(1)
    assert state["calls"] == 2  # the 401 was not retried
    assert sleeps == []


# --- pagination failures partway through current_subscribers -----------------------


def test_transient_failure_mid_pagination_completes_the_fetch():
    state = {"page2": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if page == 1:
            rows = [sub_row(i, f"u{i}@example.com") for i in range(PAGE_SIZE)]
            return data({"results": rows, "total": PAGE_SIZE + 1})
        state["page2"] += 1
        if state["page2"] == 1:
            return httpx.Response(503)
        return data({"results": [sub_row(PAGE_SIZE, "last@example.com")], "total": PAGE_SIZE + 1})

    sleeps: list[float] = []
    out = client_with(handler, sleeps).current_subscribers(1)
    assert len(out) == PAGE_SIZE + 1
    assert "last@example.com" in out


def test_persistent_failure_mid_pagination_raises_never_partial():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if page == 1:
            rows = [sub_row(i, f"u{i}@example.com") for i in range(PAGE_SIZE)]
            return data({"results": rows, "total": PAGE_SIZE + 1})
        return httpx.Response(503)

    sleeps: list[float] = []
    with pytest.raises(TargetError, match=f"HTTP 503 after {MAX_ATTEMPTS} attempts"):
        client_with(handler, sleeps).current_subscribers(1)


# --- ambiguous outcomes: a timeout after the write landed --------------------------


def test_timeout_after_landed_create_reads_back_instead_of_rewriting():
    state = {"posts": 0, "lookups": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            state["posts"] += 1
            raise httpx.ReadTimeout("read timed out")  # ...but the write landed
        state["lookups"] += 1
        assert "a@example.com" in request.url.params["query"]
        return data({"results": [sub_row(42, "a@example.com")], "total": 1})

    sleeps: list[float] = []
    client_with(handler, sleeps).create_subscriber(desired("a@example.com"), 1)
    assert state["posts"] == 1  # never replayed: the read-back confirmed it landed
    assert state["lookups"] == 1


def test_timeout_with_no_landed_write_replays_the_create():
    state = {"posts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            state["posts"] += 1
            if state["posts"] == 1:
                raise httpx.ReadTimeout("read timed out")  # and nothing landed
            return data(sub_row(7, "b@example.com"))
        return data({"results": [], "total": 0})  # read-back: not there

    sleeps: list[float] = []
    client_with(handler, sleeps).create_subscriber(desired("b@example.com"), 1)
    assert state["posts"] == 2


def test_recovered_create_feeds_the_unsubscribe_follow_up():
    """The read-back's row must be usable exactly like the lost response:
    the unsubscribe follow-up PUT runs against the recovered id."""
    state = {"posts": 0, "put_bodies": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            state["posts"] += 1
            raise httpx.ReadTimeout("read timed out")
        if request.method == "PUT":
            state["put_bodies"].append(request.read())
            return data(True)
        return data({"results": [sub_row(42, "c@example.com", "unconfirmed")], "total": 1})

    sleeps: list[float] = []
    client_with(handler, sleeps).create_subscriber(desired("c@example.com", "unsubscribed"), 1)
    assert state["posts"] == 1
    assert len(state["put_bodies"]) == 1
    assert b'"ids": [42]' in state["put_bodies"][0] or b'"ids":[42]' in state["put_bodies"][0]
