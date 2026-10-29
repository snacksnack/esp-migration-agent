"""SSO on the review gate (RC1-554): the IdP establishes who the approver is,
the approval and audit log carry it verified, and the IdP can take it back.

FakeIdp stands in for Okta behind the IdentityProvider protocol: a user
directory the test can deactivate, refresh tokens that rotate, and a switch
for an unreachable IdP. The real OidcProvider's token-endpoint handling is
tested separately against httpx.MockTransport.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from executor.cutover import render_cutover_plan
from executor.engine import execute
from executor.plan import build_plan
from executor.reconcile import reconcile, render_markdown
from ingest.mailchimp import load_bundle
from mapping.gate import approve, load_approved, load_spec
from review_ui import auth
from review_ui.api import create_app
from review_ui.audit import AuditLog, verify_chain
from review_ui.config import ReviewUiSettings
from review_ui.oidc import IdpUnavailable, OidcProvider, SignIn, SsoError
from specs.model import ApproverIdentity, MappingSpec, SpecStatus
from tests.fake_target import FakeTarget

DRAFT = Path("specs/drafts/mailchimp_to_listmonk.yaml")
APPROVED = Path("specs/approved/mailchimp_to_listmonk.yaml")
AUDIENCE = Path("fixtures/mailchimp_export/audience")
SUPPLEMENT = Path("fixtures/api_supplement")
AS_OF = datetime(2026, 10, 24, tzinfo=UTC)
ISSUER = "https://customer.okta.example"


class FakeIdp:
    name = "Okta"

    def __init__(self):
        self.active = {"dana": True, "sam": True}
        self.assigned = {"dana", "sam"}
        self.issue_refresh_tokens = True
        self.down = False
        self.refresh_calls = 0
        self.revoked: list[str] = []
        self._live: dict[str, str] = {}  # refresh token -> user
        self._n = 0

    def _mint(self, user: str) -> str:
        self._n += 1
        token = f"rt-{user}-{self._n}"
        self._live[token] = user
        return token

    async def begin(self, request: Request) -> RedirectResponse:
        user = request.query_params.get("as", "dana")  # test hook: who logs in at the IdP
        return RedirectResponse(f"/api/auth/sso/callback?code={user}")

    async def complete(self, request: Request) -> SignIn:
        user = request.query_params["code"]
        if user not in self.assigned or not self.active.get(user):
            raise SsoError("Okta sign-in failed: User is not assigned to the client application.")
        if not self.issue_refresh_tokens:
            raise SsoError("Okta issued no refresh token")
        identity = ApproverIdentity(
            method="oidc",
            verified=True,
            issuer=ISSUER,
            subject=f"00u-{user}",
            email=f"{user}@customer.example",
            name=user.title() + " Ops",
        )
        return SignIn(identity=identity, refresh_token=self._mint(user))

    def refresh(self, refresh_token: str) -> str | None:
        self.refresh_calls += 1
        if self.down:
            raise IdpUnavailable("connect timeout")
        user = self._live.pop(refresh_token, None)  # rotation: old token is spent
        if user is None or not self.active[user]:
            return None
        return self._mint(user)

    def revoke(self, refresh_token: str) -> None:
        self.revoked.append(refresh_token)
        self._live.pop(refresh_token, None)


def sso_settings(tmp_path: Path, **overrides) -> ReviewUiSettings:
    values = {
        "review_password": "let-me-in",
        "review_session_secret": "test-secret",
        "oidc_issuer": ISSUER,
        "oidc_client_id": "client",
        "oidc_client_secret": "secret",
        "oidc_recheck_seconds": 0,  # ask the IdP before every write
        "audit_log": tmp_path / "audit.jsonl",
        "_env_file": None,
    }
    values.update(overrides)
    return ReviewUiSettings(**values)


def make_client(tmp_path: Path, idp: FakeIdp, settings: ReviewUiSettings | None = None):
    spec_path = tmp_path / DRAFT.name
    if not spec_path.exists():
        spec_path.write_text(DRAFT.read_text())
    app = create_app(
        spec_path,
        audience_dir=AUDIENCE,
        supplement_dir=SUPPLEMENT,
        account_tz="America/New_York",
        target="listmonk",
        as_of=AS_OF,
        approved_dir=tmp_path / "approved",
        settings=settings or sso_settings(tmp_path),
        provider=idp,
    )
    return TestClient(app, follow_redirects=False)


def sso_login(client: TestClient, user: str = "dana") -> httpx.Response:
    start = client.get(f"/api/auth/sso/start?as={user}")
    assert start.status_code in (302, 303, 307)
    return client.get(start.headers["location"])


def resolve_everything(client: TestClient) -> None:
    kinds = {
        "field_mappings": ("field", "source_field"),
        "segment_mappings": ("segment", "source_segment"),
        "consent_mappings": ("consent", "source_status"),
    }
    for list_attr, (kind, source_attr) in kinds.items():
        spec = client.get("/api/spec").json()["spec"]
        for i, m in enumerate(spec[list_attr]):
            if m["status"] not in ("proposed", "flagged"):
                continue
            resp = client.patch(
                f"/api/mappings/{kind}/{i}",
                json={"source": m[source_attr], "status": "edited", "target": "resolved"},
            )
            assert resp.status_code == 200, resp.json()


def events(tmp_path: Path) -> list[str]:
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    return [json.loads(line)["event"] for line in lines]


def test_sso_sign_in_establishes_a_verified_identity(tmp_path: Path):
    client = make_client(tmp_path, FakeIdp())
    me = client.get("/api/me").json()
    assert me["sso_provider"] == "Okta" and me["authenticated"] is False
    done = sso_login(client)
    assert done.status_code == 303 and done.headers["location"] == "/"
    me = client.get("/api/me").json()
    assert me["authenticated"] is True
    assert me["identity"]["email"] == "dana@customer.example"
    assert me["identity"]["verified"] is True
    assert events(tmp_path) == ["sign_in"]


def test_verified_approval_carries_the_idp_identity_not_a_typed_name(tmp_path: Path):
    client = make_client(tmp_path, FakeIdp())
    sso_login(client)
    resolve_everything(client)
    resp = client.post("/api/approve", json={"approved_by": "Someone Else"})
    assert resp.status_code == 200, resp.json()
    assert resp.json()["approved_by"] == "Dana Ops"
    spec = load_approved(tmp_path / "approved" / DRAFT.name)  # still a valid signed spec
    assert spec.approval.approved_by == "Dana Ops"
    assert spec.approval.identity.subject == "00u-dana"
    assert spec.approval.identity.issuer == ISSUER
    assert "verified via OIDC" in spec.approval.identity_line()
    # Resolutions + the approval are all attributed to the federated identity.
    log = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    approved = [r for r in log if r["event"] == "spec_approved"]
    assert approved[0]["actor"]["subject"] == "00u-dana"
    assert approved[0]["detail"]["content_sha256"] == spec.approval.content_sha256
    resolved = [r for r in log if r["event"] == "mapping_resolved"]
    assert len(resolved) == 16 and all(r["actor"]["verified"] for r in resolved)
    assert verify_chain(tmp_path / "audit.jsonl") == []


def test_deactivating_the_user_at_the_idp_ends_write_access(tmp_path: Path):
    idp = FakeIdp()
    client = make_client(tmp_path, idp)
    sso_login(client)
    ok = client.patch(
        "/api/mappings/consent/0", json={"source": "subscribed", "status": "approved"}
    )
    assert ok.status_code == 200
    idp.active["dana"] = False  # IT offboards Dana in Okta
    resp = client.patch(
        "/api/mappings/consent/1", json={"source": "unsubscribed", "status": "approved"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"].startswith("Okta no longer vouches for your sign-in")
    assert load_spec(tmp_path / DRAFT.name).consent_mappings[1].status.value != "approved"
    assert client.get("/api/me").json()["authenticated"] is False  # signed out for good
    assert events(tmp_path)[-1] == "access_revoked"
    # And she cannot sign back in: the IdP refuses her at the door.
    back = sso_login(client)
    assert "sso_error=" in back.headers["location"]
    assert events(tmp_path)[-1] == "sign_in_refused"


def test_recheck_window_limits_idp_round_trips(tmp_path: Path):
    idp = FakeIdp()
    client = make_client(tmp_path, idp, sso_settings(tmp_path, oidc_recheck_seconds=3600))
    sso_login(client)
    resolve_everything(client)
    assert idp.refresh_calls == 0  # confirmed at sign-in, inside the window since


def test_unreachable_idp_fails_closed(tmp_path: Path):
    idp = FakeIdp()
    client = make_client(tmp_path, idp)
    sso_login(client)
    idp.down = True
    resp = client.patch(
        "/api/mappings/consent/0", json={"source": "subscribed", "status": "approved"}
    )
    assert resp.status_code == 503
    assert "nothing was changed" in resp.json()["detail"]
    assert load_spec(tmp_path / DRAFT.name).consent_mappings[0].status.value == "proposed"
    idp.down = False  # transient: the session survives the outage
    assert client.get("/api/me").json()["authenticated"] is True


def test_restart_forgets_the_refresh_token_and_asks_for_a_fresh_sign_in(tmp_path: Path):
    client = make_client(tmp_path, FakeIdp())
    sso_login(client)
    cookie = client.cookies.get(auth.COOKIE_NAME)
    restarted = make_client(tmp_path, FakeIdp())  # same secret, empty session store
    restarted.cookies.set(auth.COOKIE_NAME, cookie)
    resp = restarted.patch(
        "/api/mappings/consent/0", json={"source": "subscribed", "status": "approved"}
    )
    assert resp.status_code == 401


def test_unassigned_user_and_missing_refresh_token_are_refused(tmp_path: Path):
    idp = FakeIdp()
    idp.assigned.discard("sam")
    client = make_client(tmp_path, idp)
    refused = sso_login(client, "sam")
    assert "not%20assigned" in refused.headers["location"]
    assert client.get("/api/me").json()["authenticated"] is False
    idp.issue_refresh_tokens = False
    assert "sso_error=" in sso_login(client, "dana").headers["location"]
    assert events(tmp_path) == ["sign_in_refused", "sign_in_refused"]


def test_sign_out_revokes_the_refresh_token_at_the_idp(tmp_path: Path):
    idp = FakeIdp()
    client = make_client(tmp_path, idp)
    sso_login(client)
    assert client.post("/api/logout").json() == {"ok": True}
    assert idp.revoked and client.get("/api/me").json()["authenticated"] is False
    assert events(tmp_path) == ["sign_in", "sign_out"]


def test_password_fallback_is_recorded_unverified_beside_sso(tmp_path: Path):
    client = make_client(tmp_path, FakeIdp())
    assert client.post("/api/login", json={"password": "let-me-in"}).status_code == 200
    resolve_everything(client)
    resp = client.post("/api/approve", json={"approved_by": "Recruiter Demo"})
    assert resp.status_code == 200
    assert resp.json()["identity"] == {
        "method": "password", "verified": False,
        "issuer": None, "subject": None, "email": None, "name": None,
    }
    approval = load_spec(tmp_path / "approved" / DRAFT.name).approval
    assert approval.approved_by == "Recruiter Demo"
    assert approval.identity_line().startswith("unverified")


def test_sso_only_deploy_has_no_password_path(tmp_path: Path):
    client = make_client(tmp_path, FakeIdp(), sso_settings(tmp_path, review_password=None))
    assert client.post("/api/login", json={"password": "let-me-in"}).status_code == 401
    me = client.get("/api/me").json()
    assert me["password_enabled"] is False and me["auth_enabled"] is True


def test_anonymous_gate_refusals_do_not_write_the_audit_log(tmp_path: Path):
    client = make_client(tmp_path, FakeIdp())
    for _ in range(3):
        assert client.post("/api/approve", json={"approved_by": "Visitor"}).status_code == 409
    assert not (tmp_path / "audit.jsonl").exists()
    assert client.get("/api/audit").json() == {"records": []}
    sso_login(client)
    assert client.post("/api/approve", json={}).status_code == 409
    assert events(tmp_path) == ["sign_in", "approval_refused"]
    newest = client.get("/api/audit").json()["records"][0]
    assert newest["event"] == "approval_refused"


def test_audit_chain_detects_edits_and_deletions(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for event in ("sign_in", "mapping_resolved", "spec_approved"):
        log.record(event, None)
    assert verify_chain(path) == []
    # A reopened log continues the chain rather than restarting it.
    AuditLog(path).record("sign_out", None)
    assert verify_chain(path) == []
    lines = path.read_text().splitlines()
    edited = json.loads(lines[1])
    edited["event"] = "sign_in"
    path.write_text("\n".join([lines[0], json.dumps(edited), *lines[2:]]) + "\n")
    assert any("altered" in p for p in verify_chain(path))
    path.write_text("\n".join([lines[0], *lines[2:]]) + "\n")  # drop a record
    assert any("chain broken" in p for p in verify_chain(path))


def test_identity_free_approvals_round_trip_byte_identical():
    # Committed approved specs predate RC1-554: no identity key may appear.
    text = APPROVED.read_text()
    assert MappingSpec.from_yaml(text).to_yaml() == text


def test_cutover_plan_and_reconcile_report_name_the_verified_approver():
    spec = load_spec(APPROVED)
    identity = ApproverIdentity(
        method="oidc", verified=True, issuer=ISSUER, subject="00u-dana",
        email="dana@customer.example", name="Dana Ops",
    )
    unsigned = spec.model_copy(update={"approval": None, "status": SpecStatus.DRAFT})
    signed = approve(unsigned, approved_by="Dana Ops", identity=identity)
    bundle = load_bundle(AUDIENCE, SUPPLEMENT, "America/New_York")
    plan = build_plan(bundle, signed)
    expected = "verified via OIDC (https://customer.okta.example, sub 00u-dana"
    assert expected in render_cutover_plan(plan, signed, ramp="none")
    target = FakeTarget()
    execute(plan, target)
    report_md = render_markdown(reconcile(bundle, signed, target))
    assert "## Approval\n\nSpec signed off by Dana Ops; " + expected in report_md
    # A pre-RC1-554 approval says plainly that the name is unverified text.
    legacy = render_markdown(reconcile(bundle, spec, target))
    assert "identity not recorded" in legacy


def okta_provider(handler) -> OidcProvider:
    settings = sso_settings(Path("/nonexistent"))
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return OidcProvider(settings, http=http)


def discovery_or(handler):
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": ISSUER,
                "token_endpoint": f"{ISSUER}/oauth2/v1/token",
                "revocation_endpoint": f"{ISSUER}/oauth2/v1/revoke",
            })
        return handler(request)
    return route


def test_oidc_provider_refresh_semantics():
    def rotating(request):
        assert b"grant_type=refresh_token" in request.content
        assert request.headers["authorization"].startswith("Basic ")
        return httpx.Response(200, json={"refresh_token": "rt-2", "access_token": "at"})

    assert okta_provider(discovery_or(rotating)).refresh("rt-1") == "rt-2"
    same = okta_provider(discovery_or(lambda r: httpx.Response(200, json={"access_token": "a"})))
    assert same.refresh("rt-1") == "rt-1"  # no rotation: keep the token we have
    deactivated = okta_provider(
        discovery_or(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    )
    assert deactivated.refresh("rt-1") is None
    with pytest.raises(IdpUnavailable):
        okta_provider(discovery_or(lambda r: httpx.Response(502))).refresh("rt-1")

    def unreachable(request):
        raise httpx.ConnectError("down")

    with pytest.raises(IdpUnavailable):
        okta_provider(unreachable).refresh("rt-1")
    okta_provider(unreachable).revoke("rt-1")  # best effort: never raises
