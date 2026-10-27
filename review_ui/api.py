"""FastAPI backend for the review-gate UI (RC1-560).

Three resources, all wrapping existing modules:

- GET  /api/spec                      the draft spec, re-read from disk
- PATCH /api/mappings/{kind}/{index}  resolve one mapping; writes the YAML
- POST /api/approve                   mapping.gate.approve, refusals verbatim
- GET  /api/report                    the DiscoveryReport (charts context)

The draft YAML on disk stays the document of record: every PATCH round-trips
through MappingSpec validation and rewrites the file, so the schema's
flag-don't-guess and approval invariants hold for UI edits exactly as they do
for hand edits. Approval is mapping.gate.approve with the named owner - a
refusal (open questions remain) returns 409 carrying the gate's own message,
and it is public - the gate speaks before identity matters (RC1-562). Writes
are the approver's alone: resolving and a successful sign-off answer 401
without the session cookie, with a message that says why, and nothing ever
changes on disk for an anonymous visitor.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from discovery.report import DiscoveryReport, build_report
from discovery.risks import sort_risks
from ingest.mailchimp import load_bundle
from mapping.gate import GateError, approve, load_spec
from mapping.risks import spec_risks
from review_ui import auth
from review_ui.config import ReviewUiSettings
from specs.model import MappingSpec, MappingStatus
from telemetry import count

FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

# URL segment -> (spec list attribute, source-identity attribute).
MAPPING_KINDS = {
    "field": ("field_mappings", "source_field"),
    "segment": ("segment_mappings", "source_segment"),
    "consent": ("consent_mappings", "source_status"),
}


class ResolveRequest(BaseModel):
    """A reviewer resolution: the mapping leaves proposed/flagged for
    approved/edited, with the decision recorded in the target/transform."""

    # Length caps: resolving is a public write on the hosted demo (RC1-562),
    # so the payload is bounded; real reviews never approach these.
    source: str = Field(
        min_length=1, max_length=500, description="source identity, guards stale indexes"
    )
    status: Literal[MappingStatus.APPROVED, MappingStatus.EDITED]
    target: str | None = Field(default=None, max_length=4000)
    transform: str | None = Field(default=None, max_length=4000)


class ApproveRequest(BaseModel):
    approved_by: str = Field(min_length=1)


class LoginBody(BaseModel):
    # Module-level, not a closure local: `from __future__ import annotations`
    # stringifies hints, and FastAPI resolves them from module globals - a
    # local class silently degrades to a query parameter (school-search lesson).
    password: str


def _load(spec_path: Path) -> MappingSpec:
    try:
        return load_spec(spec_path)
    except GateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def create_app(
    spec_path: Path,
    *,
    audience_dir: Path,
    supplement_dir: Path,
    account_tz: str,
    target: str,
    as_of: datetime,
    approved_dir: Path = Path("specs/approved"),
    settings: ReviewUiSettings | None = None,
) -> FastAPI:
    settings = settings or ReviewUiSettings()
    app = FastAPI(title="ESP migration review gate", docs_url=None, redoc_url=None)
    report_cache: list[DiscoveryReport] = []  # built once; the bundle never changes mid-session

    # No blanket middleware (RC1-562): reading is public, and the approve
    # endpoint runs the gate BEFORE checking identity so its refusal is
    # public too. Writes - resolving and a successful sign-off - require the
    # approver cookie; anonymous visitors toggle everything in the UI but
    # nothing ever changes on disk.

    def require_approver(request: Request, detail: str) -> None:
        if settings.auth_enabled and not auth.token_valid(
            settings, request.cookies.get(auth.COOKIE_NAME)
        ):
            raise HTTPException(status_code=401, detail=detail)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "app": "esp-review-gate"}

    @app.post("/api/login")
    def login(body: LoginBody) -> Response:
        if not settings.auth_enabled or not auth.password_matches(settings, body.password):
            return JSONResponse({"detail": "wrong password"}, status_code=401)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            auth.COOKIE_NAME,
            auth.make_token(settings),
            max_age=settings.review_session_days * 86400,
            httponly=True,
            samesite="lax",
            secure=settings.environment == "production",
        )
        return response

    @app.get("/api/me")
    def me(request: Request) -> dict:
        ok = not settings.auth_enabled or auth.token_valid(
            settings, request.cookies.get(auth.COOKIE_NAME)
        )
        return {"authenticated": ok, "auth_enabled": settings.auth_enabled}

    @app.get("/api/spec")
    def get_spec() -> dict:
        return {"path": str(spec_path), "spec": _load(spec_path).model_dump(mode="json")}

    @app.patch("/api/mappings/{kind}/{index}")
    def resolve_mapping(kind: str, index: int, body: ResolveRequest, request: Request) -> dict:
        require_approver(
            request,
            "This is a live demo: anyone can review, but saving a change to the "
            "migration spec needs the approver sign-in.",
        )
        if kind not in MAPPING_KINDS:
            raise HTTPException(status_code=404, detail=f"unknown mapping kind {kind!r}")
        list_attr, source_attr = MAPPING_KINDS[kind]
        spec = _load(spec_path)
        mappings = getattr(spec, list_attr)
        if not 0 <= index < len(mappings):
            raise HTTPException(status_code=404, detail=f"no {kind} mapping at index {index}")
        mapping = mappings[index]
        if getattr(mapping, source_attr) != body.source:
            raise HTTPException(
                status_code=409,
                detail=f"{kind} mapping {index} is {getattr(mapping, source_attr)!r}, "
                f"not {body.source!r}; the spec changed on disk - reload and retry",
            )
        updated = mapping.model_dump(mode="json")
        updated["status"] = body.status.value
        updated["open_question"] = None  # resolving answers the question
        if body.target is not None:
            updated[mapping.target_attr] = body.target
        if body.transform is not None:
            if kind != "field":
                raise HTTPException(
                    status_code=422, detail="transform applies to field mappings only"
                )
            updated["transform"] = body.transform
        content = spec.model_dump(mode="json")
        content[list_attr][index] = updated
        try:
            new_spec = MappingSpec.model_validate(content)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        spec_path.write_text(new_spec.to_yaml())
        return {"path": str(spec_path), "spec": new_spec.model_dump(mode="json")}

    @app.post("/api/approve")
    def approve_spec(body: ApproveRequest, request: Request) -> dict:
        spec = _load(spec_path)
        try:
            approved = approve(spec, approved_by=body.approved_by)
        except GateError as exc:
            count("gate_approval", tags={"outcome": "refused"})
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # Identity is checked only once the gate would pass: the refusal above
        # is the demo's centerpiece and stays public, and nothing has been
        # written yet, so this 401 signs off on nothing (RC1-562).
        require_approver(
            request, "every gate passes - but signing off needs the approver sign-in"
        )
        count("gate_approval", tags={"outcome": "approved"})
        out = approved_dir / spec_path.name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(approved.to_yaml())
        return {
            "out": str(out),
            "approved_by": approved.approval.approved_by,
            "approved_at": approved.approval.approved_at.isoformat(),
            "content_sha256": approved.approval.content_sha256,
        }

    @app.get("/api/report")
    def get_report() -> DiscoveryReport:
        if not report_cache:
            bundle = load_bundle(audience_dir, supplement_dir, account_tz)
            report_cache.append(build_report(bundle, as_of=as_of, target=target))
        report = report_cache[0].model_copy()
        # Same merge as `discovery.report --spec`: the under-review spec's open
        # questions join the severity-ranked register the reviewer sees.
        report.risks = sort_risks([*report.risks, *spec_risks(_load(spec_path))])
        return report

    if FRONTEND_DIST.is_dir():  # single-process demo mode; dev uses the Vite proxy
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")

    return app
