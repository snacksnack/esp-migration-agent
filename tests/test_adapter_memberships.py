"""RC1-573: the adapters' membership contract on the wire. Each request body
here encodes a behavior probed against a live Listmonk (ADR-0018):
PUT /subscribers/{id} REPLACES list memberships, so an update must re-send
every list the record is on; bulk "add" attaches without touching the
global status. httpx.MockTransport only - no network."""

from __future__ import annotations

import json

import httpx

from executor.brevo import BrevoClient
from executor.listmonk import LOOKUP_BATCH, ListmonkClient
from executor.plan import DesiredSubscriber
from executor.target import TargetSubscriber


def desired(email: str = "x@example.com") -> DesiredSubscriber:
    return DesiredSubscriber(
        email=email, name="X", status="enabled", subscription="confirmed",
        subscribable=True, suppressed=False, attribs={},
    )


def existing(list_ids: list[int], status: str = "enabled") -> TargetSubscriber:
    return TargetSubscriber(
        id=42, email="x@example.com", name="X", status=status,
        subscription="", attribs={}, list_ids=list_ids,
    )


class Recorder:
    def __init__(self, respond):
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)

    def bodies(self, method: str) -> list[dict]:
        return [json.loads(r.read()) for r in self.requests if r.method == method]


def listmonk(recorder: Recorder) -> ListmonkClient:
    return ListmonkClient("http://lm.test", "u", "p", transport=httpx.MockTransport(recorder))


# --- Listmonk -------------------------------------------------------------------------


def test_listmonk_update_preserves_every_other_list():
    rec = Recorder(lambda r: httpx.Response(200, json={"data": {"id": 42}}))
    listmonk(rec).update_subscriber(existing([3, 7]), desired(), list_id=9)
    (body,) = rec.bodies("PUT")
    assert body["lists"] == [3, 7, 9]  # not [9]: that would drop lists 3 and 7


def test_listmonk_create_sends_only_the_target_list():
    rec = Recorder(lambda r: httpx.Response(200, json={"data": {"id": 1}}))
    listmonk(rec).create_subscriber(desired(), list_id=9)
    (body,) = rec.bodies("POST")
    assert body["lists"] == [9]


def test_listmonk_attach_is_membership_only():
    rec = Recorder(lambda r: httpx.Response(200, json={"data": True}))
    listmonk(rec).attach_subscriber(existing([3], status="blocklisted"), list_id=9)
    (body,) = rec.bodies("PUT")
    assert rec.requests[0].url.path == "/api/subscribers/lists"
    assert body == {
        "ids": [42], "action": "add", "target_list_ids": [9], "status": "unsubscribed",
    }  # "status" here is the new list subscription; nothing sets the global status


def test_listmonk_lookup_batches_and_matches_case_insensitively():
    def respond(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        rows = []
        if "'mixed@example.com'" in query:
            rows = [{"id": 5, "email": "Mixed@Example.com", "name": "M", "status": "blocklisted",
                     "lists": [{"id": 3, "subscription_status": "unsubscribed"}],
                     "attribs": {}}]
        return httpx.Response(200, json={"data": {"results": rows, "total": len(rows)}})

    rec = Recorder(respond)
    emails = [f"u{i}@example.com" for i in range(LOOKUP_BATCH * 2)] + ["MIXED@example.com"]
    found = listmonk(rec).lookup_subscribers(emails)

    assert len(rec.requests) == 3  # 401 addresses in batches of 200
    assert all("LOWER(subscribers.email) IN" in r.url.params["query"] for r in rec.requests)
    assert set(found) == {"mixed@example.com"}
    hit = found["mixed@example.com"]
    assert (hit.status, hit.subscription, hit.list_ids) == ("blocklisted", "", [3])


def test_listmonk_current_subscribers_report_all_memberships():
    row = {"id": 5, "email": "a@example.com", "name": "A", "status": "enabled",
           "lists": [{"id": 3, "subscription_status": "unsubscribed"},
                     {"id": 9, "subscription_status": "confirmed"}], "attribs": {}}
    rec = Recorder(lambda r: httpx.Response(200, json={"data": {"results": [row], "total": 1}}))
    (sub,) = listmonk(rec).current_subscribers(9).values()
    assert (sub.subscription, sub.list_ids) == ("confirmed", [3, 9])


def test_listmonk_segment_count_keeps_an_or_inside_the_list():
    rec = Recorder(lambda r: httpx.Response(200, json={"data": {"results": [], "total": 0}}))
    listmonk(rec).count_query(9, "a = 1 OR b = 2")
    params = rec.requests[0].url.params
    assert (params["list_id"], params["query"]) == ("9", "(a = 1 OR b = 2)")


# --- Brevo ----------------------------------------------------------------------------


def test_brevo_attach_never_sends_the_blacklist_flag():
    rec = Recorder(lambda r: httpx.Response(201, json={"contacts": {"success": []}}))
    BrevoClient("k", transport=httpx.MockTransport(rec)).attach_subscriber(
        existing([3], status="blocklisted"), list_id=9
    )
    (body,) = rec.bodies("POST")
    assert rec.requests[0].url.path.endswith("/contacts/lists/9/contacts/add")
    assert body == {"emails": ["x@example.com"]}


def test_brevo_lookup_walks_the_account_and_keeps_global_consent():
    def respond(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        rows = [
            {"id": i, "email": f"c{i}@example.com", "emailBlacklisted": i == 1500,
             "listIds": [3], "attributes": {"ESP_CONSENT": "blocklisted"} if i == 1500 else {}}
            for i in range(offset, min(offset + 1000, 1600))
        ]
        return httpx.Response(200, json={"contacts": rows, "count": 1600})

    rec = Recorder(respond)
    found = BrevoClient("k", transport=httpx.MockTransport(rec)).lookup_subscribers(
        ["C1500@example.com", "absent@example.com"]
    )
    assert len(rec.requests) == 2  # paged past the first 1,000
    assert set(found) == {"c1500@example.com"}
    assert found["c1500@example.com"].status == "blocklisted"


def test_brevo_lookup_of_nothing_makes_no_call():
    rec = Recorder(lambda r: httpx.Response(500))
    assert BrevoClient("k", transport=httpx.MockTransport(rec)).lookup_subscribers([]) == {}
    assert rec.requests == []
