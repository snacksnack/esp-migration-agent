"""Who the approver is (RC1-561 -> RC1-554).

The session is Starlette's signed cookie (itsdangerous over the same
ESP_REVIEW_SESSION_SECRET the RC1-561 HMAC token used), holding the
ApproverIdentity the sign-in established plus an expiry. Two ways in:

- the shared demo password: write access, identity unverified - the name on
  an approval is whatever the approver typed;
- OIDC SSO: the IdP vouches for the identity, the approval carries it
  verified, and review_ui.oidc re-confirms it with the IdP before writes.

The cookie is signed, not encrypted: it holds nothing secret. The refresh
token that lets the server re-confirm an SSO identity stays server-side,
keyed by the session's random sid (review_ui.oidc.SessionStore).
"""

from __future__ import annotations

import hmac
import secrets
import time

from fastapi import Request
from pydantic import ValidationError

from review_ui.config import ReviewUiSettings
from specs.model import ApproverIdentity

COOKIE_NAME = "esp_review_session"

PASSWORD_IDENTITY = ApproverIdentity(method="password", verified=False)


def password_matches(settings: ReviewUiSettings, password: str) -> bool:
    if not settings.review_password:
        return False
    return hmac.compare_digest(password.encode(), settings.review_password.encode())


def sign_in(request: Request, identity: ApproverIdentity, ttl_seconds: int) -> str:
    """Start a fresh session for identity; returns its sid. Clearing first
    means a password session upgraded to SSO (or vice versa) keeps nothing
    from the old one."""
    request.session.clear()
    sid = secrets.token_urlsafe(24)
    request.session.update(
        {
            "sid": sid,
            "exp": int(time.time()) + ttl_seconds,
            "identity": identity.model_dump(mode="json"),
        }
    )
    return sid


def sign_out(request: Request) -> None:
    request.session.clear()


def session_sid(request: Request) -> str | None:
    return request.session.get("sid")


def current_identity(request: Request) -> ApproverIdentity | None:
    """The signed-in identity, or None when absent, expired, or unreadable.
    Expiry is checked here, not just by the cookie's max-age, because a
    client can replay a cookie past its max-age."""
    exp = request.session.get("exp")
    raw = request.session.get("identity")
    if not isinstance(exp, int) or exp < time.time() or not isinstance(raw, dict):
        return None
    try:
        return ApproverIdentity.model_validate(raw)
    except ValidationError:
        return None
