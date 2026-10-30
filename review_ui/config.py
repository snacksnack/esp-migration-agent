"""Review-UI settings (RC1-561, RC1-554), following the repo's per-module pattern.

Two ways to become the approver, either or both configured:

- Shared demo password (RC1-561): ESP_REVIEW_PASSWORD. Grants write access
  but proves nothing about who you are - approvals made this way are
  recorded as unverified.
- OIDC single sign-on against the customer's IdP (RC1-554): ESP_OIDC_ISSUER
  + client id/secret. The IdP vouches for the identity, approvals carry it
  verified, and the IdP can take it away again (deactivation is re-checked
  before every write).

With neither configured the gate is off entirely - local dev, CI, and the
tests run exactly as before RC1-561.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ReviewUiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ESP_", env_file=".env", extra="ignore")

    environment: str = "development"

    # Shared demo password. Unset = no password path. The session cookie is
    # signed with review_session_secret, so the secret must be stable across
    # restarts in production or every viewer is logged out on each deploy.
    review_password: str | None = Field(default=None, repr=False)
    review_session_secret: str = Field(default="dev-secret-not-for-prod", repr=False)
    review_session_days: int = 30

    # OIDC SSO (RC1-554). The issuer's discovery document supplies every
    # endpoint; the redirect URI is derived from public_url so it matches the
    # one registered at the IdP exactly (behind Fly's proxy the request's own
    # scheme reads http).
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = Field(default=None, repr=False)
    oidc_provider_name: str = "Okta"
    public_url: str = "http://localhost:8542"
    # Signed-in identity is re-confirmed with the IdP (a refresh-token grant,
    # which fails once the user is deactivated) when the last check is older
    # than this. Writes are rare, so a short window costs little.
    oidc_recheck_seconds: int = 30
    # SSO sessions are shorter than the demo password's: the identity is the
    # point, and re-authenticating is one click through the IdP.
    oidc_session_hours: int = 8

    # Append-only, hash-chained log of gate decisions (RC1-554). Ephemeral on
    # the hosted demo like the spec itself (ADR-0011); every record is also
    # logged to stdout.
    audit_log: Path = Path(".review_audit.jsonl")

    # Durable state (RC1-576, ADR-0022): with a database URL the audit log and
    # SSO sessions live in Postgres (Neon on the hosted demo) instead of the
    # file above and process memory. The URL is the least-privilege app role,
    # never the owner. The token key encrypts refresh tokens at rest (Fernet;
    # generate with `python -m review_ui.db_setup new-key`) and is required
    # whenever the database is.
    database_url: str | None = Field(default=None, repr=False)
    session_token_key: str | None = Field(default=None, repr=False)

    @property
    def sso_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.review_password) or self.sso_enabled

    @property
    def oidc_redirect_uri(self) -> str:
        return f"{self.public_url.rstrip('/')}/api/auth/sso/callback"
