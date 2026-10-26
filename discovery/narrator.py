"""LLM narration of the computed audience profile (RC1-544).

The one place in Phase 1 where the model speaks - and it only narrates.
Every number is computed in Python first (discovery/profile.py) and handed
over pre-aggregated, percentages included, so the narration never has to do
arithmetic. Phase 5's evals check exactly this: numbers cited in the prose
must exist in the payload.

Needs ESP_ANTHROPIC_API_KEY; the report renders without narration when the
key is absent, and tests never call this module's network path.
"""

from __future__ import annotations

import json

import anthropic
from pydantic_settings import BaseSettings, SettingsConfigDict

from discovery.inventory import Inventory
from discovery.profile import AudienceProfile
from discovery.risks import Risk

SYSTEM_PROMPT = """You write the "who this audience is" section of an email-platform
migration discovery report, for a professional-services audience.

Rules, in priority order:
1. Use ONLY numbers that appear in the JSON payload (counts and the
   precomputed percentages). Never derive, extrapolate, or round further.
2. If a signal is missing or degenerate (the payload says so), say that
   plainly instead of speculating around it.
3. Three short paragraphs maximum: composition (domains, geography),
   tenure and consent, then engagement and notable risks.
4. Plain prose. No headers, no bullet lists, no marketing language."""


class NarrationError(RuntimeError):
    pass


class NarratorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ESP_", env_file=".env", extra="ignore")

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-5-5"


def _pct(part: int, total: int) -> float:
    return round(100 * part / total, 1) if total else 0.0


def narration_payload(
    profile: AudienceProfile, inventory: Inventory, risks: list[Risk]
) -> dict:
    """Aggregates plus precomputed percentages: the narration's entire world."""
    total = profile.total
    return {
        "as_of": profile.as_of.date().isoformat(),
        "total_members_in_export": total,
        "consent": {
            bucket: {"count": n, "pct": _pct(n, total)} for bucket, n in profile.consent.items()
        },
        "domain_class": {
            cls: {"count": n, "pct": _pct(n, total)} for cls, n in profile.domain_class.items()
        },
        "top_domains": profile.top_domains,
        "top_states": dict(list(profile.states.items())[:5]),
        "tenure_cohorts": {
            cohort: {"count": n, "pct": _pct(n, total)}
            for cohort, n in profile.tenure_cohorts.items()
        },
        "member_rating_distribution": {str(k): v for k, v in profile.member_rating.items()},
        "suppressed_members": profile.suppressed,
        "high_severity_risks": [r.title for r in risks if r.severity == "high"],
        "segments": [s.name for s in inventory.segments],
    }


def narrate(profile: AudienceProfile, inventory: Inventory, risks: list[Risk]) -> str:
    settings = NarratorSettings()
    if not settings.anthropic_api_key:
        raise NarrationError("ESP_ANTHROPIC_API_KEY is not set (see .env.example)")
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    payload = narration_payload(profile, inventory, risks)
    try:
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=2000,  # the narration is three short paragraphs by contract
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": "Narrate this audience profile:\n"
                    + json.dumps(payload, indent=2),
                }
            ],
        )
    except anthropic.RateLimitError as exc:
        raise NarrationError(f"rate limited: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise NarrationError(f"API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise NarrationError(f"connection error: {exc}") from exc
    if response.stop_reason == "refusal":
        raise NarrationError("narration request was refused")
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise NarrationError("empty narration response")
    return text
