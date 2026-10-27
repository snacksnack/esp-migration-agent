"""The demo's sign-off gate (RC1-561 -> RC1-562, school-search auth core).

The model under test: reading is public - the hosted demo is linked from the
portfolio and must browse without a password - and the gate's refusal is
public too, because watching approve refuse IS the demo. Writes are the
approver's alone: resolving and a successful sign-off answer 401 anonymously
with an explanatory message, and nothing ever changes on disk. With no
password configured everything is open (local dev and CI unchanged).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from mapping.gate import load_spec
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


GATED = ReviewUiSettings(
    review_password="let-me-in", review_session_secret="test-secret", _env_file=None
)
OPEN = ReviewUiSettings(review_password=None, _env_file=None)


def test_everything_open_without_a_password(tmp_path: Path):
    client = make_client(tmp_path, OPEN)
    assert client.get("/api/spec").status_code == 200
    assert client.get("/api/me").json() == {"authenticated": True, "auth_enabled": False}
    # Login is meaningless with the gate off and must not mint a cookie.
    assert client.post("/api/login", json={"password": "anything"}).status_code == 401
    # Sign-off needs no login either: local use is exactly pre-RC1-561.
    resolve_everything(client)
    assert client.post("/api/approve", json={"approved_by": "Reid"}).status_code == 200


def test_reading_is_public_with_the_gate_on(tmp_path: Path):
    client = make_client(tmp_path, GATED)
    assert client.get("/api/spec").status_code == 200
    assert client.get("/api/report").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/me").json() == {"authenticated": False, "auth_enabled": True}


def test_anonymous_resolve_changes_nothing(tmp_path: Path):
    client = make_client(tmp_path, GATED)
    resp = client.patch(
        "/api/mappings/consent/0", json={"source": "subscribed", "status": "approved"}
    )
    assert resp.status_code == 401
    assert "saving a change" in resp.json()["detail"]
    on_disk = load_spec(tmp_path / DRAFT.name).consent_mappings[0]
    assert on_disk.status.value == "proposed"  # the draft is untouched


def test_gate_refusal_is_public(tmp_path: Path):
    client = make_client(tmp_path, GATED)
    resp = client.post("/api/approve", json={"approved_by": "Visitor"})
    assert resp.status_code == 409  # the gate speaks before identity matters
    assert resp.json()["detail"].startswith("cannot approve: 16 unresolved")


def test_sign_off_still_401s_anonymously_when_every_gate_passes(tmp_path: Path):
    client = make_client(tmp_path, GATED)
    assert client.post("/api/login", json={"password": "let-me-in"}).status_code == 200
    resolve_everything(client)
    client.cookies.clear()  # back to an anonymous visitor, gates all passing
    resp = client.post("/api/approve", json={"approved_by": "Visitor"})
    assert resp.status_code == 401
    assert "signing off needs the approver sign-in" in resp.json()["detail"]
    # Nothing was written: no approved file, draft still unapproved on disk.
    assert not (tmp_path / "approved").exists()
    assert load_spec(tmp_path / DRAFT.name).approval is None


def test_login_then_resolve_and_sign_off(tmp_path: Path):
    client = make_client(tmp_path, GATED)
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"password": "let-me-in"}).status_code == 200
    resolve_everything(client)
    resp = client.post("/api/approve", json={"approved_by": "Reid Collins"})
    assert resp.status_code == 200
    assert (tmp_path / "approved" / DRAFT.name).exists()
    assert client.get("/api/me").json() == {"authenticated": True, "auth_enabled": True}


def test_resolve_payload_length_caps(tmp_path: Path):
    client = make_client(tmp_path, GATED)
    client.post("/api/login", json={"password": "let-me-in"})
    resp = client.patch(
        "/api/mappings/field/0",
        json={"source": "EMAIL", "status": "edited", "target": "x" * 4001},
    )
    assert resp.status_code == 422


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
