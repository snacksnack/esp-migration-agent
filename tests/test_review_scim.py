"""SCIM push deprovisioning on the review gate (RC1-575, ADR-0023).

The requests below are the ones Okta's provisioning connector sends: a
userName filter before create, POST to create, PATCH active=false to
deactivate (PUT for custom app integrations), never DELETE. The point under
test is push, not pull: in the deactivation tests the FakeIdp still says the
user is active and the re-check window is an hour, so the only thing that can
end the session is the SCIM call.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from review_ui.audit import verify_chain
from review_ui.scim import PATCH_SCHEMA, USER_SCHEMA
from tests.test_review_sso import FakeIdp, make_client, sso_login, sso_settings

TOKEN = "scim-test-token"
OKTA = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/scim+json"}


def scim_client(tmp_path: Path, idp: FakeIdp, **overrides) -> TestClient:
    values = {"scim_token": TOKEN, "oidc_recheck_seconds": 3600, **overrides}
    return make_client(tmp_path, idp, sso_settings(tmp_path, **values))


def okta_user(user: str, active: bool = True, external_id: str | None = None) -> dict:
    """The body Okta POSTs/PUTs (abridged to what this server reads)."""
    body = {
        "schemas": [USER_SCHEMA],
        "userName": f"{user}@customer.example",
        "name": {"givenName": user.title(), "familyName": "Ops"},
        "emails": [{"primary": True, "value": f"{user}@customer.example", "type": "work"}],
        "displayName": f"{user.title()} Ops",
        "active": active,
    }
    if external_id:
        body["externalId"] = external_id
    return body


def provision(client: TestClient, user: str, **kwargs) -> str:
    found = client.get(
        "/scim/v2/Users", params={"filter": f'userName eq "{user}@customer.example"'},
        headers=OKTA,
    )
    assert found.status_code == 200 and found.json()["totalResults"] == 0
    created = client.post("/scim/v2/Users", json=okta_user(user, **kwargs), headers=OKTA)
    assert created.status_code == 201, created.json()
    assert created.headers["content-type"].startswith("application/scim+json")
    return created.json()["id"]


def deactivate_patch() -> dict:
    operation = {"op": "replace", "value": {"active": False}}
    return {"schemas": [PATCH_SCHEMA], "Operations": [operation]}


def events(tmp_path: Path) -> list[str]:
    return [
        line.split('"event": "')[1].split('"')[0]
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]


def me(client: TestClient) -> dict:
    return client.get("/api/me").json()


def test_scim_is_off_without_a_token_and_refuses_a_wrong_one(tmp_path):
    off = make_client(tmp_path, FakeIdp())
    assert off.get("/scim/v2/Users", headers=OKTA).status_code in (404, 405)
    on = scim_client(tmp_path, FakeIdp())
    assert on.get("/scim/v2/Users").status_code == 401
    assert on.get("/scim/v2/Users", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert on.get("/scim/v2/Users", headers={"Authorization": TOKEN}).status_code == 200


def test_okta_create_flow_lists_reads_and_refuses_duplicates(tmp_path):
    client = scim_client(tmp_path, FakeIdp())
    user_id = provision(client, "dana", external_id="00u-dana")
    found = client.get(
        "/scim/v2/Users", params={"filter": 'userName eq "DANA@customer.example"'}, headers=OKTA
    ).json()
    assert found["totalResults"] == 1 and found["Resources"][0]["id"] == user_id
    got = client.get(f"/scim/v2/Users/{user_id}", headers=OKTA).json()
    assert got["userName"] == "dana@customer.example" and got["active"] is True
    assert got["externalId"] == "00u-dana" and got["meta"]["resourceType"] == "User"
    dup = client.post("/scim/v2/Users", json=okta_user("dana"), headers=OKTA)
    assert dup.status_code == 409 and dup.json()["scimType"] == "uniqueness"
    missing = client.get("/scim/v2/Users/nope", headers=OKTA)
    assert missing.status_code == 404 and missing.json()["status"] == "404"
    assert events(tmp_path) == ["scim_provisioned"]


def test_okta_deactivation_ends_the_session_with_no_write_attempt(tmp_path):
    idp = FakeIdp()
    client = scim_client(tmp_path, idp)
    sam = TestClient(client.app, follow_redirects=False)  # same server, own browser
    dana_id = provision(client, "dana")
    sso_login(client)
    sso_login(sam, "sam")
    assert me(client)["identity"]["email"] == "dana@customer.example"

    resp = client.patch(f"/scim/v2/Users/{dana_id}", json=deactivate_patch(), headers=OKTA)
    assert resp.status_code == 200 and resp.json()["active"] is False

    # Okta's own refresh check would still say yes: the push did this.
    assert idp.active["dana"] is True and idp.refresh_calls == 0
    after = me(client)  # a page load or poll, not a write
    assert after["authenticated"] is False and after["revoked_by"] == "Okta"
    assert me(client)["revoked_by"] is None  # said once, then just signed out
    assert me(sam)["identity"]["email"] == "sam@customer.example"  # others untouched
    assert events(tmp_path)[-2:] == ["scim_deactivated", "access_revoked"]
    assert '"sessions_ended": 1' in (tmp_path / "audit.jsonl").read_text()
    assert verify_chain(tmp_path / "audit.jsonl") == []


def test_put_with_active_false_is_a_deactivation_too(tmp_path):
    # Okta sends PUT, not PATCH, for custom (AIW) app integrations.
    client = scim_client(tmp_path, FakeIdp())
    dana_id = provision(client, "dana")
    sso_login(client)
    resp = client.put(
        f"/scim/v2/Users/{dana_id}", json=okta_user("dana", active=False), headers=OKTA
    )
    assert resp.status_code == 200
    assert me(client)["authenticated"] is False


def test_sessions_match_on_the_okta_user_id_when_the_email_differs(tmp_path):
    client = scim_client(tmp_path, FakeIdp())
    body = okta_user("dana", external_id="00u-dana")
    body["userName"] = body["emails"][0]["value"] = "d.ops@other.example"
    user_id = client.post("/scim/v2/Users", json=body, headers=OKTA).json()["id"]
    sso_login(client)  # signs in as dana@customer.example, subject 00u-dana
    client.patch(f"/scim/v2/Users/{user_id}", json=deactivate_patch(), headers=OKTA)
    assert me(client)["authenticated"] is False


def test_deprovisioned_users_cannot_sign_in_until_reactivated(tmp_path):
    idp = FakeIdp()
    client = scim_client(tmp_path, idp)
    dana_id = provision(client, "dana")
    client.patch(f"/scim/v2/Users/{dana_id}", json=deactivate_patch(), headers=OKTA)
    refused = sso_login(client)
    assert "sso_error=" in refused.headers["location"]
    assert "deprovisioned" in refused.headers["location"]
    assert idp.revoked  # the refresh token we were handed is handed back
    reactivate = {
        "schemas": [PATCH_SCHEMA],
        "Operations": [{"op": "replace", "path": "active", "value": "True"}],
    }
    assert client.patch(
        f"/scim/v2/Users/{dana_id}", json=reactivate, headers=OKTA
    ).json()["active"] is True
    sso_login(client)
    assert me(client)["authenticated"] is True
    assert events(tmp_path) == [
        "scim_provisioned", "scim_deactivated", "sign_in_refused", "scim_reactivated", "sign_in",
    ]


def test_profile_updates_round_trip_and_stay_out_of_the_log(tmp_path):
    client = scim_client(tmp_path, FakeIdp())
    user_id = provision(client, "dana")
    patch = {
        "schemas": [PATCH_SCHEMA],
        "Operations": [
            {"op": "replace", "path": "name.familyName", "value": "Operations"},
            {"op": "remove", "path": "displayName"},
        ],
    }
    got = client.patch(f"/scim/v2/Users/{user_id}", json=patch, headers=OKTA).json()
    assert got["name"] == {"givenName": "Dana", "familyName": "Operations"}
    assert "displayName" not in got and got["active"] is True
    assert events(tmp_path) == ["scim_provisioned"]


@pytest.mark.parametrize(
    ("body", "scim_type"),
    [
        ({"Operations": [{"op": "replace", "value": {"active": False}}]}, "invalidSyntax"),
        ({"schemas": [PATCH_SCHEMA], "Operations": []}, "invalidSyntax"),
        ({"schemas": [PATCH_SCHEMA], "Operations": [{"op": "move"}]}, "invalidSyntax"),
        (
            {"schemas": [PATCH_SCHEMA],
             "Operations": [{"op": "replace", "path": "active", "value": "maybe"}]},
            "invalidValue",
        ),
        (
            {"schemas": [PATCH_SCHEMA],
             "Operations": [{"op": "replace", "path": 'emails[type eq "work"].value',
                             "value": "x@y.z"}]},
            "invalidPath",
        ),
    ],
)
def test_malformed_patches_fail_loudly_and_change_nothing(tmp_path, body, scim_type):
    client = scim_client(tmp_path, FakeIdp())
    user_id = provision(client, "dana")
    sso_login(client)
    resp = client.patch(f"/scim/v2/Users/{user_id}", json=body, headers=OKTA)
    assert resp.status_code == 400 and resp.json()["scimType"] == scim_type
    assert me(client)["authenticated"] is True


def test_unsupported_filters_are_refused_not_ignored(tmp_path):
    client = scim_client(tmp_path, FakeIdp())
    provision(client, "dana")
    resp = client.get(
        "/scim/v2/Users", params={"filter": 'emails co "dana"'}, headers=OKTA
    )
    assert resp.status_code == 400 and resp.json()["scimType"] == "invalidFilter"


def test_delete_ends_sessions_and_removes_the_record(tmp_path):
    client = scim_client(tmp_path, FakeIdp())
    user_id = provision(client, "dana")
    sso_login(client)
    assert client.delete(f"/scim/v2/Users/{user_id}", headers=OKTA).status_code == 204
    assert client.get(f"/scim/v2/Users/{user_id}", headers=OKTA).status_code == 404
    assert me(client)["authenticated"] is False
    assert events(tmp_path)[-3:] == ["scim_deactivated", "scim_deleted", "access_revoked"]


def test_production_refuses_scim_without_a_database(tmp_path):
    with pytest.raises(ValueError, match="ESP_SCIM_TOKEN needs ESP_DATABASE_URL"):
        scim_client(tmp_path, FakeIdp(), environment="production")
