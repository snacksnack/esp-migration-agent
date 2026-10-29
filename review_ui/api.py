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

RC1-554 (ADR-0021) puts the customer's IdP in front of those writes: OIDC
sign-in (review_ui.oidc) establishes a verified identity, the approval and
the audit log carry it, and the IdP is asked again before each write - a
user deactivated at the IdP loses write access at their next click. The
shared password stays as the demo's fallback and is recorded as exactly
what it is: unverified.

- GET  /api/auth/sso/start, /api/auth/sso/callback   the OIDC handshake
- POST /api/login, /api/logout                        password in, anyone out
- GET  /api/audit                                     the gate's decision log
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.sessions import SessionMiddleware

from discovery.report import DiscoveryReport, build_report
from discovery.risks import sort_risks
from ingest.mailchimp import load_bundle
from mapping.gate import GateError, approve, load_spec
from mapping.risks import spec_risks
from review_ui import auth
from review_ui.audit import AuditLog
from review_ui.config import ReviewUiSettings
from review_ui.oidc import (
    IdentityProvider,
    IdpUnavailable,
    OidcProvider,
    SessionStore,
    SsoError,
)
from specs.model import ApproverIdentity, MappingSpec, MappingStatus
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
    # Free text for password and open-mode sign-off; ignored under SSO, where
    # the IdP's identity is the name (a typed name never overrides it).
    approved_by: str | None = Field(default=None, min_length=1, max_length=200)


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
    provider: IdentityProvider | None = None,
) -> FastAPI:
    settings = settings or ReviewUiSettings()
    if provider is None and settings.sso_enabled:
        provider = OidcProvider(settings)
    sessions = SessionStore()
    audit = AuditLog(settings.audit_log)
    app = FastAPI(title="ESP migration review gate", docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.review_session_secret,
        session_cookie=auth.COOKIE_NAME,
        max_age=settings.review_session_days * 86400,
        same_site="lax",
        https_only=settings.environment == "production",
    )
    report_cache: list[DiscoveryReport] = []  # built once; the bundle never changes mid-session

    # No blanket auth middleware (RC1-562): reading is public, and the
    # approve endpoint runs the gate BEFORE checking identity so its refusal
    # is public too. Writes - resolving and a successful sign-off - require
    # a signed-in approver; anonymous visitors toggle everything in the UI
    # but nothing ever changes on disk.

    def live_identity(request: Request) -> ApproverIdentity | None:
        """The session's identity, re-confirmed with the IdP for SSO sessions
        (RC1-554). An SSO identity the IdP no longer vouches for is signed out
        and audited here, so every caller sees None."""
        identity = auth.current_identity(request)
        if identity is None or identity.method != "oidc":
            # A password session only counts while the password path exists.
            if identity is not None and not settings.review_password:
                return None
            return identity
        sid = auth.session_sid(request)
        if provider is None or sid is None:
            auth.sign_out(request)
            return None
        try:
            ok = sessions.confirm(sid, provider, settings.oidc_recheck_seconds)
        except IdpUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail=f"could not confirm your identity with {provider.name} ({exc}); "
                "nothing was changed - try again shortly",
            ) from exc
        if not ok:
            request.state.revoked_by = provider.name
            auth.sign_out(request)
            audit.record(
                "access_revoked",
                identity,
                reason=(
                    f"{provider.name} no longer vouches for this session "
                    "(user deactivated, token revoked, or server restarted)"
                ),
            )
            return None
        return identity

    def require_approver(request: Request, detail: str) -> ApproverIdentity | None:
        """The approver's identity, or 401. None only with the gate off."""
        if not settings.auth_enabled:
            return None
        identity = live_identity(request)
        if identity is None:
            revoked_by = getattr(request.state, "revoked_by", None)
            if revoked_by:
                detail = (
                    f"{revoked_by} no longer vouches for your sign-in (account "
                    "deactivated or session revoked), so nothing was changed. "
                    "Sign in again to continue."
                )
            raise HTTPException(status_code=401, detail=detail)
        return identity

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "app": "esp-review-gate"}

    @app.post("/api/login")
    def login(body: LoginBody, request: Request) -> Response:
        if not settings.review_password or not auth.password_matches(settings, body.password):
            return JSONResponse({"detail": "wrong password"}, status_code=401)
        auth.sign_in(request, auth.PASSWORD_IDENTITY, settings.review_session_days * 86400)
        audit.record("sign_in", auth.PASSWORD_IDENTITY)
        return JSONResponse({"ok": True})

    @app.post("/api/logout")
    def logout(request: Request) -> dict:
        identity = auth.current_identity(request)
        sid = auth.session_sid(request)
        refresh_token = sessions.pop(sid) if sid else None
        if refresh_token and provider is not None:
            provider.revoke(refresh_token)
        auth.sign_out(request)
        if identity is not None:
            audit.record("sign_out", identity)
        return {"ok": True}

    @app.get("/api/auth/sso/start")
    async def sso_start(request: Request) -> Response:
        if provider is None:
            raise HTTPException(status_code=404, detail="single sign-on is not configured")
        return await provider.begin(request)

    @app.get("/api/auth/sso/callback")
    async def sso_callback(request: Request) -> Response:
        if provider is None:
            raise HTTPException(status_code=404, detail="single sign-on is not configured")
        try:
            signin = await provider.complete(request)
        except SsoError as exc:
            audit.record("sign_in_refused", None, provider=provider.name, reason=str(exc))
            return RedirectResponse(f"/?sso_error={quote(str(exc))}", status_code=303)
        sid = auth.sign_in(request, signin.identity, settings.oidc_session_hours * 3600)
        sessions.put(sid, signin.refresh_token)
        audit.record("sign_in", signin.identity, provider=provider.name)
        return RedirectResponse("/", status_code=303)

    @app.get("/api/me")
    def me(request: Request) -> dict:
        identity = live_identity(request) if settings.auth_enabled else None
        return {
            "authenticated": not settings.auth_enabled or identity is not None,
            "auth_enabled": settings.auth_enabled,
            "password_enabled": bool(settings.review_password),
            "sso_provider": provider.name if provider is not None else None,
            "identity": identity.model_dump(mode="json") if identity else None,
        }

    @app.get("/api/audit")
    def get_audit(limit: int = 100) -> dict:
        return {"records": audit.read(min(max(limit, 1), 500))}

    @app.get("/api/spec")
    def get_spec() -> dict:
        return {"path": str(spec_path), "spec": _load(spec_path).model_dump(mode="json")}

    @app.patch("/api/mappings/{kind}/{index}")
    def resolve_mapping(kind: str, index: int, body: ResolveRequest, request: Request) -> dict:
        identity = require_approver(
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
        audit.record(
            "mapping_resolved",
            identity,
            kind=kind,
            index=index,
            source=body.source,
            status=body.status.value,
            target=body.target,
            had_open_question=mapping.open_question,
        )
        return {"path": str(spec_path), "spec": new_spec.model_dump(mode="json")}

    @app.post("/api/approve")
    def approve_spec(body: ApproveRequest, request: Request) -> dict:
        spec = _load(spec_path)
        # Run the gate first with a placeholder name: its refusal is the
        # demo's centerpiece and stays public, before identity matters.
        try:
            approve(spec, approved_by=body.approved_by or "pending sign-in")
        except GateError as exc:
            count("gate_approval", tags={"outcome": "refused"})
            # Audited for signed-in sessions only: the refusal is public, and
            # anonymous clicks must not be able to flood the decision log.
            refused_by = auth.current_identity(request)
            if refused_by is not None:
                audit.record("approval_refused", refused_by, reason=str(exc))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # Nothing has been written yet, so this 401 signs off on nothing (RC1-562).
        identity = require_approver(
            request, "every gate passes - but signing off needs the approver sign-in"
        )
        if identity is not None and identity.verified:
            approved_by = identity.name or identity.email or identity.subject
        elif body.approved_by:
            approved_by = body.approved_by
        else:
            raise HTTPException(status_code=422, detail="approved_by is required")
        approved = approve(spec, approved_by=approved_by, identity=identity)
        count("gate_approval", tags={"outcome": "approved"})
        out = approved_dir / spec_path.name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(approved.to_yaml())
        audit.record(
            "spec_approved",
            identity,
            approved_by=approved_by,
            content_sha256=approved.approval.content_sha256,
            out=str(out),
        )
        return {
            "out": str(out),
            "approved_by": approved.approval.approved_by,
            "approved_at": approved.approval.approved_at.isoformat(),
            "content_sha256": approved.approval.content_sha256,
            "identity": identity.model_dump(mode="json") if identity else None,
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
