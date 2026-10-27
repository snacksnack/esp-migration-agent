"""The review UI's shared-password gate (RC1-561, school-search pattern).

Two postures under test: with no password the gate is off and nothing about
RC1-560 behavior changes; with a password every /api route except login/me
answers 401 until the HMAC cookie is presented, and the cookie round-trips
through the login flow.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from review_ui import auth
from review_ui.api import create_app
from review_ui.config import ReviewUiSettings

DRAFT = Path("specs/drafts/mailchimp_to_listmonk.yaml")
AS_OF = datetime(2026, 10, 24, tzinfo=UTC)


def make_client(tmp_path: Path, settings: ReviewUiSettings) -> TestClient:
    spec_path = tmp_path / DRAFT.name
    spec_path.write_text(DRAFT.read_text())
    app = create_app(
        spec_path,
        audience_dir=Path("fixtures/mailchimp_export/audience"),
        supplement_dir=Path("fixtures/api_supplement"),
        account_tz="America/New_York",
        target="listmonk",
        as_of=AS_OF,
        approved_dir=tmp_path / "approved",
        settings=settings,
    )
    return TestClient(app)


GATED = ReviewUiSettings(
    review_password="let-me-in", review_session_secret="test-secret", _env_file=None
)
OPEN = ReviewUiSettings(review_password=None, _env_file=None)


def test_gate_off_without_a_password(tmp_path: Path):
    client = make_client(tmp_path, OPEN)
    assert client.get("/api/spec").status_code == 200
    body = client.get("/api/me").json()
    assert body == {"authenticated": True, "auth_enabled": False}
    # Login is meaningless with the gate off and must not mint a cookie.
    assert client.post("/api/login", json={"password": "anything"}).status_code == 401


def test_gate_on_requires_the_cookie(tmp_path: Path):
    client = make_client(tmp_path, GATED)
    assert client.get("/api/spec").status_code == 401
    assert client.post("/api/approve", json={"approved_by": "x"}).status_code == 401
    assert (
        client.patch(
            "/api/mappings/field/0", json={"source": "EMAIL", "status": "approved"}
        ).status_code
        == 401
    )
    # healthz and the auth probe stay open for the Fly check and the SPA.
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/me").json() == {"authenticated": False, "auth_enabled": True}


def test_login_round_trip(tmp_path: Path):
    client = make_client(tmp_path, GATED)
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    resp = client.post("/api/login", json={"password": "let-me-in"})
    assert resp.status_code == 200
    assert auth.COOKIE_NAME in resp.cookies
    # TestClient carries the cookie jar forward; the gate now opens.
    assert client.get("/api/spec").status_code == 200
    assert client.get("/api/me").json() == {"authenticated": True, "auth_enabled": True}


def test_token_signature_and_expiry():
    token = auth.make_token(GATED)
    assert auth.token_valid(GATED, token)
    assert not auth.token_valid(GATED, None)
    assert not auth.token_valid(GATED, "garbage")
    exp, sig = token.split(".", 1)
    assert not auth.token_valid(GATED, f"{exp}.{'0' * len(sig)}")
    # A token signed with a different secret never validates.
    other = ReviewUiSettings(
        review_password="let-me-in", review_session_secret="other-secret", _env_file=None
    )
    assert not auth.token_valid(other, token)
    # An expired timestamp fails even with a valid signature.
    past = int(time.time()) - 10
    assert not auth.token_valid(GATED, f"{past}.{auth._sign('test-secret', past)}")


def test_password_matches_constant_shapes():
    assert auth.password_matches(GATED, "let-me-in")
    assert not auth.password_matches(GATED, "LET-ME-IN")
    assert not auth.password_matches(OPEN, "let-me-in")


@pytest.mark.parametrize("path", ["/api/report", "/api/spec"])
def test_every_data_route_is_gated(tmp_path: Path, path: str):
    client = make_client(tmp_path, GATED)
    assert client.get(path).status_code == 401
