"""OIDC single sign-on against the customer's IdP (RC1-554, ADR-0021).

Authlib runs the authorization-code handshake (state, nonce, PKCE S256) and
validates the ID token against the issuer's JWKS; nothing here re-implements
JWT checks. What this module adds is the part a demo login usually skips:
the IdP can take the identity back. The sign-in asks for offline_access, the
refresh token stays server-side (SessionStore, keyed by the cookie's random
sid), and before a write the API re-confirms the identity with a
refresh-token grant. A deactivated or suspended user's refresh grant fails,
so the next write after an IdP offboard answers 401 instead of succeeding.

Fails closed throughout: no refresh token at sign-in refuses the sign-in
(the identity could never be re-confirmed), an unreachable IdP refuses the
write (503), and a server restart that forgets the refresh token asks for a
fresh sign-in rather than trusting the cookie alone.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass
from typing import Protocol

import httpx
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import Request
from fastapi.responses import RedirectResponse

from review_ui.config import ReviewUiSettings
from specs.model import ApproverIdentity

SCOPES = "openid email profile offline_access"


class SsoError(RuntimeError):
    """The sign-in did not produce a usable identity; message is user-facing."""


class IdpUnavailable(RuntimeError):
    """The IdP could not be asked; writes fail closed rather than trust a stale yes."""


@dataclass(frozen=True)
class SignIn:
    identity: ApproverIdentity
    refresh_token: str


class IdentityProvider(Protocol):
    name: str

    async def begin(self, request: Request) -> RedirectResponse: ...

    async def complete(self, request: Request) -> SignIn: ...

    def refresh(self, refresh_token: str) -> str | None:
        """Re-confirm with the IdP. Returns the refresh token to keep (rotated
        or the same) while the user is still active, None once the IdP says no.
        Raises IdpUnavailable when the IdP cannot be asked."""
        ...

    def revoke(self, refresh_token: str) -> None: ...


class OidcProvider:
    """Any standards OIDC issuer; Okta is the one the demo and runbook use."""

    def __init__(self, settings: ReviewUiSettings, http: httpx.Client | None = None):
        if not settings.sso_enabled:
            raise ValueError("OidcProvider needs ESP_OIDC_ISSUER, client id and client secret")
        self.name = settings.oidc_provider_name
        self._settings = settings
        self._issuer = settings.oidc_issuer.rstrip("/")
        self._http = http or httpx.Client(timeout=10)
        self._metadata: dict | None = None
        oauth = OAuth()
        oauth.register(
            name="idp",
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            server_metadata_url=f"{self._issuer}/.well-known/openid-configuration",
            client_kwargs={"scope": SCOPES, "code_challenge_method": "S256"},
        )
        self._client = oauth.create_client("idp")

    async def begin(self, request: Request) -> RedirectResponse:
        return await self._client.authorize_redirect(request, self._settings.oidc_redirect_uri)

    async def complete(self, request: Request) -> SignIn:
        try:
            token = await self._client.authorize_access_token(request)
        except OAuthError as exc:
            # e.g. access_denied when the user is not assigned to the app in
            # the IdP - the customer's assignment IS the access list.
            raise SsoError(f"{self.name} sign-in failed: {exc.description or exc.error}") from exc
        claims = token.get("userinfo") or {}
        refresh_token = token.get("refresh_token")
        if not claims.get("sub"):
            raise SsoError(f"{self.name} returned no ID token subject")
        if not refresh_token:
            raise SsoError(
                f"{self.name} issued no refresh token, so access could never be "
                "re-checked; enable the Refresh Token grant on the app integration"
            )
        identity = ApproverIdentity(
            method="oidc",
            verified=True,
            issuer=claims.get("iss", self._issuer),
            subject=claims["sub"],
            email=claims.get("email"),
            name=claims.get("name"),
        )
        return SignIn(identity=identity, refresh_token=refresh_token)

    def _discovery(self) -> dict:
        if self._metadata is None:
            try:
                resp = self._http.get(f"{self._issuer}/.well-known/openid-configuration")
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise IdpUnavailable(f"{self.name} discovery failed: {exc}") from exc
            self._metadata = resp.json()
        return self._metadata

    def _auth(self) -> tuple[str, str]:
        return (self._settings.oidc_client_id, self._settings.oidc_client_secret)

    def refresh(self, refresh_token: str) -> str | None:
        endpoint = self._discovery()["token_endpoint"]
        try:
            resp = self._http.post(
                endpoint,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": SCOPES,
                },
                auth=self._auth(),
            )
        except httpx.HTTPError as exc:
            raise IdpUnavailable(f"{self.name} unreachable: {exc}") from exc
        if resp.status_code in (400, 401):  # invalid_grant: revoked, user inactive
            return None
        if resp.status_code != 200:
            raise IdpUnavailable(f"{self.name} token endpoint answered {resp.status_code}")
        return resp.json().get("refresh_token") or refresh_token

    def revoke(self, refresh_token: str) -> None:
        # Best effort: sign-out has already cleared the local session.
        with contextlib.suppress(IdpUnavailable, httpx.HTTPError):
            endpoint = self._discovery().get("revocation_endpoint")
            if endpoint:
                self._http.post(
                    endpoint,
                    data={"token": refresh_token, "token_type_hint": "refresh_token"},
                    auth=self._auth(),
                )


@dataclass
class Grant:
    refresh_token: str
    checked_at: float  # wall-clock epoch seconds, comparable across restarts


class SessionStore:
    """sid -> refresh token + when the IdP last confirmed it.

    This base class holds the confirmation rule and keeps grants in memory
    (dev, CI, deploys without a database): a restart forgets them, which
    means a fresh sign-in - the safe direction. review_ui.pgstore
    (RC1-576) overrides the three storage hooks to keep them in Postgres,
    encrypted, so deploys stop signing SSO users out."""

    def __init__(self):
        self._grants: dict[str, Grant] = {}
        self._lock = threading.Lock()

    # Storage hooks: overridden by PostgresSessionStore.
    def _load(self, sid: str) -> Grant | None:
        with self._lock:
            return self._grants.get(sid)

    def _save(self, sid: str, grant: Grant, ttl_seconds: int | None) -> None:
        with self._lock:
            self._grants[sid] = grant

    def _drop(self, sid: str) -> Grant | None:
        with self._lock:
            return self._grants.pop(sid, None)

    def put(self, sid: str, refresh_token: str, ttl_seconds: int) -> None:
        self._save(sid, Grant(refresh_token, time.time()), ttl_seconds)

    def pop(self, sid: str) -> str | None:
        grant = self._drop(sid)
        return grant.refresh_token if grant else None

    def confirm(self, sid: str, provider: IdentityProvider, max_age: float) -> bool:
        """True while the IdP still vouches for this session. Asks the IdP
        only when the last confirmation is older than max_age."""
        grant = self._load(sid)
        if grant is None:
            return False
        if time.time() - grant.checked_at < max_age:
            return True
        rotated = provider.refresh(grant.refresh_token)
        if rotated is None:
            self._drop(sid)
            return False
        self._save(sid, Grant(rotated, time.time()), None)
        return True
