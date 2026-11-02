"""SCIM 2.0 push deprovisioning for the review gate (RC1-575, ADR-0023).

RC1-554 made the IdP the owner of identity, but revocation was pull: a
deactivated user lost access at their next click, when the refresh-token
re-check failed. Here the IdP pushes instead. Okta's provisioning connector
calls /scim/v2/Users as users are assigned, updated and deactivated; a
deactivation (Okta sends active=false by PATCH, or by PUT for custom app
integrations; it never sends DELETE) ends every session that user holds,
right then, and the audit log records the push.

Only the /Users surface Okta's connector uses is implemented: filter by
userName, create, read, replace, patch, and DELETE for other clients. No
Groups. Users are matched to sessions by Okta user id (externalId, the OIDC
subject) or by email (userName / emails), case-insensitively.

The connector authenticates with a bearer token (ESP_SCIM_TOKEN). The
directory lives in Postgres alongside the sessions it revokes
(review_ui.pgstore); the in-memory base class here serves dev and tests.
"""

from __future__ import annotations

import hmac
import re
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from review_ui.config import ReviewUiSettings
from review_ui.oidc import SessionStore
from specs.model import ApproverIdentity

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCIM_JSON = "application/scim+json"

# Attributes the server owns; never stored from a request body.
SERVER_OWNED = {"id", "meta", "schemas", "password"}

# The one filter Okta's connector sends: userName eq "dana@example.com".
USERNAME_FILTER = re.compile(r'^\s*userName\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.IGNORECASE)


class ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: str | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type


class UserNameTaken(Exception):
    pass


@dataclass
class ScimUser:
    id: str
    user_name: str
    active: bool
    attributes: dict[str, Any]  # as the IdP sent them, minus SERVER_OWNED
    created: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def external_id(self) -> str | None:
        value = self.attributes.get("externalId")
        return value if isinstance(value, str) and value else None

    def emails(self) -> set[str]:
        found = {self.user_name.lower()} if "@" in self.user_name else set()
        for entry in self.attributes.get("emails") or []:
            if isinstance(entry, dict) and isinstance(entry.get("value"), str):
                found.add(entry["value"].lower())
        return found

    def resource(self, base_url: str) -> dict:
        return {
            "schemas": [USER_SCHEMA],
            **self.attributes,
            "id": self.id,
            "userName": self.user_name,
            "active": self.active,
            "meta": {
                "resourceType": "User",
                "created": self.created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "lastModified": self.updated.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "location": f"{base_url}/Users/{self.id}",
            },
        }


class ScimDirectory:
    """Users the IdP has provisioned. In memory here (dev, tests); a restart
    forgets them, so the IdP's next update to a known id gets a 404 - which
    is why production requires the Postgres subclass."""

    def __init__(self):
        self._users: dict[str, ScimUser] = {}
        self._lock = threading.Lock()

    # Storage hooks: overridden by PostgresScimDirectory.
    def get(self, user_id: str) -> ScimUser | None:
        with self._lock:
            return self._users.get(user_id)

    def by_user_name(self, user_name: str) -> ScimUser | None:
        with self._lock:
            return next(
                (u for u in self._users.values() if u.user_name.lower() == user_name.lower()),
                None,
            )

    def page(self, start: int, count: int) -> tuple[int, list[ScimUser]]:
        """1-based start, as SCIM counts."""
        with self._lock:
            users = sorted(self._users.values(), key=lambda u: u.created)
        return len(users), users[start - 1 : start - 1 + count]

    def insert(self, user: ScimUser) -> None:
        with self._lock:
            if any(u.user_name.lower() == user.user_name.lower() for u in self._users.values()):
                raise UserNameTaken(user.user_name)
            self._users[user.id] = user

    def update(self, user: ScimUser) -> None:
        with self._lock:
            clash = any(
                u.id != user.id and u.user_name.lower() == user.user_name.lower()
                for u in self._users.values()
            )
            if clash:
                raise UserNameTaken(user.user_name)
            self._users[user.id] = user

    def delete(self, user_id: str) -> None:
        with self._lock:
            self._users.pop(user_id, None)

    def deprovisioned(self, identity: ApproverIdentity) -> ScimUser | None:
        """The directory's inactive record for this identity, if the IdP has
        pushed one; sign-in is refused while it stands."""
        with self._lock:
            users = list(self._users.values())
        return next((u for u in users if not u.active and matches(u, identity)), None)


def matches(user: ScimUser, identity: ApproverIdentity) -> bool:
    if identity.subject and user.external_id == identity.subject:
        return True
    return bool(identity.email) and identity.email.lower() in user.emails()


class AuditSink(Protocol):
    def record(self, event: str, actor: ApproverIdentity | None, **detail) -> dict: ...


def scim_error_response(request: Request, exc: Exception) -> JSONResponse:
    """Exception handler create_app registers: SCIM clients expect errors in
    the SCIM error schema, not FastAPI's {"detail": ...}."""
    if isinstance(exc, UserNameTaken):
        exc = ScimError(409, f"userName {exc} already exists", "uniqueness")
    return _error(exc)


def _error(exc: ScimError) -> JSONResponse:
    body: dict[str, Any] = {
        "schemas": [ERROR_SCHEMA],
        "detail": exc.detail,
        "status": str(exc.status),
    }
    if exc.scim_type:
        body["scimType"] = exc.scim_type
    return JSONResponse(body, status_code=exc.status, media_type=SCIM_JSON)


def _scim(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=SCIM_JSON)


async def _json_object(request: Request) -> dict:
    # Okta sends application/scim+json; read the body ourselves rather than
    # rely on content-type negotiation.
    try:
        body = await request.json()
    except ValueError as exc:
        raise ScimError(400, "request body is not JSON", "invalidSyntax") from exc
    if not isinstance(body, dict):
        raise ScimError(400, "request body must be a JSON object", "invalidSyntax")
    return body


def _active_of(value: Any) -> bool:
    # Some clients send "False"/"True" strings; anything else falsy-looking is
    # refused rather than guessed.
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ScimError(400, f"active must be a boolean, got {value!r}", "invalidValue")


def _user_name_of(body: dict) -> str:
    user_name = body.get("userName")
    if not isinstance(user_name, str) or not user_name.strip():
        raise ScimError(400, "userName is required", "invalidValue")
    return user_name.strip()


def _attributes_of(body: dict) -> dict:
    return {k: v for k, v in body.items() if k not in SERVER_OWNED | {"userName", "active"}}


def apply_patch(user: ScimUser, body: dict) -> ScimUser:
    """RFC 7644 PATCH, the subset IdP connectors send: add/replace with a
    top-level or dotted path, or with no path and an object of attributes;
    remove with a path. Returns the patched copy."""
    if PATCH_SCHEMA not in (body.get("schemas") or []):
        raise ScimError(400, f"PATCH body must declare {PATCH_SCHEMA}", "invalidSyntax")
    operations = body.get("Operations")
    if not isinstance(operations, list) or not operations:
        raise ScimError(400, "PATCH needs a non-empty Operations list", "invalidSyntax")
    values: dict[str, Any] = {
        **user.attributes,
        "userName": user.user_name,
        "active": user.active,
    }
    for op in operations:
        if not isinstance(op, dict):
            raise ScimError(400, "each operation must be an object", "invalidSyntax")
        kind = str(op.get("op", "")).lower()
        path = op.get("path")
        if kind in ("add", "replace"):
            if path:
                _set_path(values, str(path), op.get("value"))
            elif isinstance(op.get("value"), dict):
                for key, value in op["value"].items():
                    _set_path(values, key, value)
            else:
                raise ScimError(400, f"{kind} without a path needs an object value", "invalidValue")
        elif kind == "remove":
            if not path:
                raise ScimError(400, "remove needs a path", "noTarget")
            _remove_path(values, str(path))
        else:
            raise ScimError(400, f"unsupported PATCH op {op.get('op')!r}", "invalidSyntax")
    return replace(
        user,
        user_name=_user_name_of(values),
        active=_active_of(values.get("active", True)),
        attributes=_attributes_of(values),
        updated=datetime.now(UTC),
    )


def _set_path(values: dict, path: str, value: Any) -> None:
    if "[" in path:
        raise ScimError(400, f"filtered PATCH paths are not supported: {path!r}", "invalidPath")
    if path.startswith(USER_SCHEMA + ":"):
        path = path[len(USER_SCHEMA) + 1 :]
    *parents, leaf = path.split(".")
    target = values
    for part in parents:
        nested = target.get(part)
        if not isinstance(nested, dict):
            nested = {}
            target[part] = nested
        target = nested
    target[leaf] = value


def _remove_path(values: dict, path: str) -> None:
    if path in ("userName", "active"):
        raise ScimError(400, f"{path} cannot be removed", "mutability")
    *parents, leaf = path.split(".")
    target = values
    for part in parents:
        target = target.get(part)
        if not isinstance(target, dict):
            return
    target.pop(leaf, None)


def scim_router(
    settings: ReviewUiSettings,
    directory: ScimDirectory,
    sessions: SessionStore,
    audit: AuditSink,
    provider_name: str,
) -> APIRouter:
    """Mounted at /scim/v2 by create_app when ESP_SCIM_TOKEN is set."""
    expected = (settings.scim_token or "").encode()
    base_url = f"{settings.public_url.rstrip('/')}/scim/v2"
    pusher = f"{provider_name} SCIM"

    def bearer(request: Request) -> None:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else header
        if not expected or not hmac.compare_digest(token.strip().encode(), expected):
            raise HTTPException(status_code=401, detail="invalid SCIM bearer token")

    router = APIRouter(dependencies=[Depends(bearer)])

    def transitioned(before: ScimUser | None, after: ScimUser) -> None:
        """Audit and act on activation changes; profile edits are not gate
        decisions and stay out of the log."""
        was_active = before.active if before is not None else None
        if was_active is not False and not after.active:
            ended = sessions.end_sessions(
                {after.external_id} if after.external_id else set(), after.emails()
            )
            audit.record(
                "scim_deactivated",
                None,
                provider=pusher,
                user_name=after.user_name,
                scim_id=after.id,
                sessions_ended=ended,
            )
        elif was_active is False and after.active:
            audit.record(
                "scim_reactivated", None, provider=pusher, user_name=after.user_name,
                scim_id=after.id,
            )
        elif before is None:
            audit.record(
                "scim_provisioned", None, provider=pusher, user_name=after.user_name,
                scim_id=after.id,
            )

    def found(user_id: str) -> ScimUser:
        user = directory.get(user_id)
        if user is None:
            raise ScimError(404, "User not found")
        return user

    @router.get("/Users")
    async def list_users(
        filter: str | None = None,  # SCIM names these query parameters
        startIndex: int = 1,
        count: int = 100,
    ) -> Response:
        start, size = max(startIndex, 1), min(max(count, 0), 200)
        if filter is not None:
            match = USERNAME_FILTER.match(filter)
            if match is None:
                raise ScimError(400, f"unsupported filter {filter!r}", "invalidFilter")
            user = directory.by_user_name(match.group(1).replace('\\"', '"'))
            users = [user] if user is not None else []
            total, users = len(users), users[start - 1 : start - 1 + size]
        else:
            total, users = directory.page(start, size)
        return _scim(
            {
                "schemas": [LIST_SCHEMA],
                "totalResults": total,
                "startIndex": start,
                "itemsPerPage": len(users),
                "Resources": [u.resource(base_url) for u in users],
            }
        )

    @router.post("/Users")
    async def create_user(request: Request) -> Response:
        body = await _json_object(request)
        user = ScimUser(
            id=str(uuid.uuid4()),
            user_name=_user_name_of(body),
            active=_active_of(body.get("active", True)),
            attributes=_attributes_of(body),
        )
        directory.insert(user)
        transitioned(None, user)
        return _scim(user.resource(base_url), status=201)

    @router.get("/Users/{user_id}")
    async def get_user(user_id: str) -> Response:
        return _scim(found(user_id).resource(base_url))

    @router.put("/Users/{user_id}")
    async def replace_user(user_id: str, request: Request) -> Response:
        before = found(user_id)
        body = await _json_object(request)
        after = replace(
            before,
            user_name=_user_name_of(body),
            active=_active_of(body.get("active", True)),
            attributes=_attributes_of(body),
            updated=datetime.now(UTC),
        )
        directory.update(after)
        transitioned(before, after)
        return _scim(after.resource(base_url))

    @router.patch("/Users/{user_id}")
    async def patch_user(user_id: str, request: Request) -> Response:
        before = found(user_id)
        after = apply_patch(before, await _json_object(request))
        directory.update(after)
        transitioned(before, after)
        return _scim(after.resource(base_url))

    @router.delete("/Users/{user_id}")
    async def delete_user(user_id: str) -> Response:
        # Okta never sends this (it deactivates); other IdPs do. Same effect
        # on sessions, then the record goes.
        before = found(user_id)
        if before.active:
            transitioned(before, replace(before, active=False))
        directory.delete(user_id)
        audit.record(
            "scim_deleted", None, provider=pusher, user_name=before.user_name, scim_id=before.id
        )
        return Response(status_code=204)

    @router.get("/ServiceProviderConfig")
    def service_provider_config() -> Response:
        unsupported = {"supported": False}
        return _scim(
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
                "patch": {"supported": True},
                "bulk": {**unsupported, "maxOperations": 0, "maxPayloadSize": 0},
                "filter": {"supported": True, "maxResults": 200},
                "changePassword": unsupported,
                "sort": unsupported,
                "etag": unsupported,
                "authenticationSchemes": [
                    {
                        "type": "oauthbearertoken",
                        "name": "Bearer token",
                        "description": "ESP_SCIM_TOKEN, sent as Authorization: Bearer",
                    }
                ],
            }
        )

    return router
