"""Review-UI settings (RC1-561), following the repo's per-module pattern.

The hosted demo's gate is the school-search shared-password pattern: one
password, an HMAC-signed expiry cookie, stdlib only. With no
ESP_REVIEW_PASSWORD set the gate is off entirely - local dev, CI, and the
tests run exactly as before RC1-561.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ReviewUiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ESP_", env_file=".env", extra="ignore")

    environment: str = "development"

    # Shared demo password. Unset = gate off (dev/CI). The session cookie is
    # an HMAC over an expiry timestamp, so the secret must be stable across
    # restarts in production or every viewer is logged out on each deploy.
    review_password: str | None = Field(default=None, repr=False)
    review_session_secret: str = Field(default="dev-secret-not-for-prod", repr=False)
    review_session_days: int = 30

    @property
    def auth_enabled(self) -> bool:
        return bool(self.review_password)
