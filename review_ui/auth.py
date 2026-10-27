"""Shared-password gate (RC1-561): one demo password, an HMAC-signed expiry
cookie. Ported from school-search app/auth.py - stdlib only (hmac over
"exp-timestamp"), no user accounts. The SPA and its assets are served to
anyone; every /api route except login/me requires the cookie. The password
protects ACCESS, not identity: approved_by stays free text, and real
named-owner identity (SSO + audit log) remains RC1-554.
"""

from __future__ import annotations

import hmac
import time
from hashlib import sha256

from review_ui.config import ReviewUiSettings

COOKIE_NAME = "esp_review_session"


def _sign(secret: str, exp: int) -> str:
    return hmac.new(secret.encode(), str(exp).encode(), sha256).hexdigest()


def make_token(settings: ReviewUiSettings) -> str:
    exp = int(time.time()) + settings.review_session_days * 86400
    return f"{exp}.{_sign(settings.review_session_secret, exp)}"


def token_valid(settings: ReviewUiSettings, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp_str, sig = token.split(".", 1)
    try:
        exp = int(exp_str)
    except ValueError:
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(sig, _sign(settings.review_session_secret, exp))


def password_matches(settings: ReviewUiSettings, password: str) -> bool:
    if not settings.review_password:
        return False
    return hmac.compare_digest(password.encode(), settings.review_password.encode())
