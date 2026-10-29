"""Review-gate UI backend (RC1-560): the API is a front end, never a bypass.

Each test drives the FastAPI layer the way the browser would and then checks
the document of record - the draft YAML on disk - and the gate semantics:
resolutions must survive MappingSpec validation, approve refuses while open
questions remain (the gate's own message, verbatim), and the approved file
passes load_approved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mapping.gate import load_approved, load_spec
from review_ui.api import create_app
from review_ui.config import ReviewUiSettings
from specs.model import MappingStatus

DRAFT = Path("specs/drafts/mailchimp_to_listmonk.yaml")
AUDIENCE = Path("fixtures/mailchimp_export/audience")
SUPPLEMENT = Path("fixtures/api_supplement")
AS_OF = datetime(2026, 10, 24, tzinfo=UTC)


@pytest.fixture
def spec_path(tmp_path: Path) -> Path:
    path = tmp_path / DRAFT.name
    path.write_text(DRAFT.read_text())
    return path


@pytest.fixture
def client(spec_path: Path, tmp_path: Path) -> TestClient:
    app = create_app(
        spec_path,
        audience_dir=AUDIENCE,
        supplement_dir=SUPPLEMENT,
        account_tz="America/New_York",
        target="listmonk",
        as_of=AS_OF,
        approved_dir=tmp_path / "approved",
        # Gate off, pinned: tests never read .env (repo convention), and this
        # file tests the API semantics, not the RC1-561 password gate.
        settings=ReviewUiSettings(
            review_password=None, audit_log=tmp_path / "audit.jsonl", _env_file=None
        ),
    )
    return TestClient(app)


def _statuses(spec_json: dict) -> list[str]:
    return [
        m["status"]
        for kind in ("field_mappings", "segment_mappings", "consent_mappings")
        for m in spec_json[kind]
    ]


def test_get_spec_returns_draft_with_open_questions(client: TestClient, spec_path: Path):
    body = client.get("/api/spec").json()
    assert body["path"] == str(spec_path)
    assert _statuses(body["spec"]).count("flagged") == 9
    assert body["spec"]["status"] == "draft"


def test_resolve_flagged_field_mapping_writes_the_yaml(client: TestClient, spec_path: Path):
    spec = client.get("/api/spec").json()["spec"]
    index = next(
        i for i, m in enumerate(spec["field_mappings"]) if m["source_field"] == "ZIP"
    )
    resp = client.patch(
        f"/api/mappings/field/{index}",
        json={
            "source": "ZIP",
            "status": "edited",
            "target": "attribs.zip",
            "transform": "Left-pad truncated ZIPs to 5 digits; non-numeric to zip_raw.",
        },
    )
    assert resp.status_code == 200
    on_disk = load_spec(spec_path).field_mappings[index]
    assert on_disk.status is MappingStatus.EDITED
    assert on_disk.target_field == "attribs.zip"
    assert on_disk.open_question is None


def test_resolve_guards_against_a_stale_index(client: TestClient):
    resp = client.patch(
        "/api/mappings/field/0",
        json={"source": "NOT_THE_FIELD", "status": "approved"},
    )
    assert resp.status_code == 409
    assert "the spec changed on disk" in resp.json()["detail"]


def test_flagged_mapping_cannot_resolve_without_a_target(client: TestClient, spec_path: Path):
    spec = client.get("/api/spec").json()["spec"]
    index = next(
        i for i, m in enumerate(spec["field_mappings"]) if m["status"] == "flagged"
    )
    source = spec["field_mappings"][index]["source_field"]
    resp = client.patch(
        f"/api/mappings/field/{index}", json={"source": source, "status": "edited"}
    )
    assert resp.status_code == 422
    assert load_spec(spec_path).field_mappings[index].status is MappingStatus.FLAGGED


def test_transform_rejected_outside_field_mappings(client: TestClient):
    resp = client.patch(
        "/api/mappings/consent/0",
        json={"source": "subscribed", "status": "approved", "transform": "nope"},
    )
    assert resp.status_code == 422


def test_unknown_kind_and_index_are_404(client: TestClient):
    assert client.patch(
        "/api/mappings/merge_tag/0", json={"source": "x", "status": "approved"}
    ).status_code == 404
    assert client.patch(
        "/api/mappings/field/99", json={"source": "x", "status": "approved"}
    ).status_code == 404


def test_approve_refuses_while_open_questions_remain(client: TestClient, tmp_path: Path):
    resp = client.post("/api/approve", json={"approved_by": "Reid Collins"})
    assert resp.status_code == 409
    assert resp.json()["detail"].startswith("cannot approve: 16 unresolved")
    assert not (tmp_path / "approved").exists()


def _resolve_everything(client: TestClient) -> None:
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
                json={
                    "source": m[source_attr],
                    "status": "edited",
                    "target": "resolved-in-test",
                },
            )
            assert resp.status_code == 200, resp.json()


def test_approve_after_resolving_everything(client: TestClient, spec_path: Path, tmp_path):
    _resolve_everything(client)
    resp = client.post("/api/approve", json={"approved_by": "Reid Collins"})
    assert resp.status_code == 200
    body = resp.json()
    out = Path(body["out"])
    assert out == tmp_path / "approved" / spec_path.name
    approved = load_approved(out)  # the only downstream door accepts the file
    assert approved.approval.approved_by == "Reid Collins"
    assert approved.approval.content_sha256 == body["content_sha256"]
    # The draft on disk is untouched by approval; the approved copy is new.
    assert load_spec(spec_path).approval is None


def test_report_merges_spec_open_questions(client: TestClient):
    resp = client.get("/api/report")
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile"]["consent"]["subscribed"] > 0
    titles = [r["title"] for r in body["risks"]]
    assert any("open mapping question" in t.lower() or "ZIP" in t for t in titles)
