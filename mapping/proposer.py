"""LLM mapping proposer (RC1-545): the model's main job in this system.

The proposer drafts the stage-4 mapping spec - field mappings, merge-tag
translation, segment-logic translation, consent semantics - each with
confidence and rationale. Ambiguity becomes a flagged mapping with an open
question, never a guess; that behavior is under eval in Phase 5.

Boundaries, same shape as discovery/narrator.py:
- `mapping_payload()` is deterministic Python and is the proposer's ENTIRE
  world: observed formats, deterministic sample values, segment definitions,
  consent-bucket counts, and the target's capability notes.
- The model returns only the four mapping lists. Python assembles the
  MappingSpec shell (version, source, target, generated_at, status=draft,
  suppression policy), so every structural field stays deterministic.
- A proposer that emits status approved/edited is a defect: ProposalError.

Needs ESP_ANTHROPIC_API_KEY; tests never call the network path.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import anthropic
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from discovery.inventory import Inventory, classify_purchase_date, classify_zip
from ingest.models import MailchimpBundle
from specs.model import MappingSpec, MappingStatus, SuppressionPolicy

# What the proposer may know about each target: the capability notes a
# solutions engineer would hold in their head. Deliberately prose - the point
# is that the model maps between CONCEPTS and flags where concepts do not meet.
TARGET_NOTES: dict[str, str] = {
    "listmonk": (
        "Listmonk stores subscribers with email, name, and a free-form JSONB "
        "attribs object (custom fields live at attribs.<key>). Segments are "
        "SQL queries over subscribers (e.g. subscribers.attribs->>'zip' LIKE "
        "'112%'); there is no engagement or rating signal to query. Subscriber "
        "status is enabled or blocklisted, plus a per-list subscription status "
        "of unconfirmed, confirmed, or unsubscribed. Templates use Go template "
        "syntax: {{ .Subscriber.Attribs.first_name }}, {{ .Subscriber.Email }}, "
        "{{ UnsubscribeURL }}, {{ MessageURL }} (hosted copy of the message)."
    ),
}

SYSTEM_PROMPT = """You translate one email platform's audience structure into \
another's, drafting a mapping spec for human review. You are the proposer; a \
human reviewer approves, edits, or answers your questions, and nothing you \
output is executed without that approval.

Rules, in priority order:
1. FLAG, DO NOT GUESS. When a mapping needs a judgment call the payload cannot
   settle - ambiguous or mixed value formats, overlapping or duplicate source
   structures, unclear consent semantics, no faithful target equivalent - set
   "status": "flagged", put the decision the reviewer must make in
   "open_question", and omit the target. A plausible guess is a defect.
2. Every field, segment, and consent mapping carries "confidence" (0.0-1.0)
   and "rationale" (one or two sentences citing facts from the payload).
3. "status" is only ever "proposed" or "flagged". Never approve or edit.
4. Be exhaustive: every source field, every segment, every merge tag in use,
   and every observed consent bucket appears exactly once. Nothing is
   silently dropped.
5. Output strict JSON only - no markdown fences, no commentary - matching:
{
  "field_mappings": [{"source_field": str, "source_type": str,
    "sample_values": [str], "target_field": str|null, "transform": str,
    "confidence": float, "rationale": str, "status": "proposed"|"flagged",
    "open_question": str|null}],
  "merge_tag_mappings": [{"source_tag": str, "target_tag": str}],
  "segment_mappings": [{"source_segment": str, "source_logic": str,
    "target_query": str|null, "confidence": float, "rationale": str,
    "status": "proposed"|"flagged", "open_question": str|null}],
  "consent_mappings": [{"source_status": str, "target_status": str|null,
    "confidence": float, "rationale": str, "status": "proposed"|"flagged",
    "open_question": str|null}]
}
For segment_mappings, "source_logic" restates the source conditions in plain
words and "target_query" is a target-dialect query over migrated attributes."""


class ProposalError(RuntimeError):
    pass


#: Bump when SYSTEM_PROMPT changes, so an eval regression is attributable to
#: the prompt rather than guessed at (RC1-548; run records carry it).
PROMPT_VERSION = "1"


class ProposerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ESP_", env_file=".env", extra="ignore")

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-5-5"


def _samples_by_format(
    values: list[str], classify: Callable[[str], str], per_format: int = 2
) -> dict[str, list[str]]:
    """Deterministic per-format samples: sorted distinct values, first N of
    each observed format class. Shows the model every shape the field takes."""
    out: dict[str, list[str]] = {}
    for value in sorted(set(values)):
        if not value:
            continue
        bucket = out.setdefault(classify(value), [])
        if len(bucket) < per_format:
            bucket.append(value)
    return out


def _flat_samples(samples: dict[str, list[str]]) -> list[str]:
    return [v for fmt in sorted(samples) for v in samples[fmt]]


def mapping_payload(bundle: MailchimpBundle, inventory: Inventory, target: str) -> dict:
    """Deterministic input to the proposer: its entire world (cf. narrator)."""
    if target not in TARGET_NOTES:
        raise ProposalError(f"unknown target {target!r}; known: {sorted(TARGET_NOTES)}")
    members = bundle.all_members
    zip_samples = _samples_by_format([m.zip_code for m in members], classify_zip)
    date_samples = _samples_by_format([m.last_purchase for m in members], classify_purchase_date)
    ratings = sorted({m.member_rating for m in members})
    return {
        "source": "mailchimp",
        "target": target,
        "target_notes": TARGET_NOTES[target],
        "fields": [
            {"source_field": "EMAIL", "source_type": "email", "notes": "primary key"},
            {"source_field": "FNAME", "source_type": "text",
             "empty_count": inventory.missing_fname},
            {"source_field": "LNAME", "source_type": "text"},
            {"source_field": "ZIP", "source_type": "text",
             "observed_formats": inventory.merge_field_formats["Zip code"],
             "sample_values": _flat_samples(zip_samples)},
            {"source_field": "STATE", "source_type": "text",
             "sample_values": sorted({m.state for m in members if m.state})[:3]},
            {"source_field": "LASTPURCH", "source_type": "text",
             "observed_formats": inventory.merge_field_formats["Last purchase"],
             "sample_values": _flat_samples(date_samples)},
            {"source_field": "MEMBER_RATING", "source_type": "integer",
             "notes": "source-computed engagement score",
             "distinct_values_observed": ratings},
            {"source_field": "OPTIN_TIME", "source_type": "datetime",
             "notes": "parsed to UTC at ingest"},
            {"source_field": "CONFIRM_TIME", "source_type": "datetime",
             "notes": "parsed to UTC at ingest"},
        ],
        "segments": [
            {
                "name": s.name,
                "match": s.match,
                "conditions": [c.model_dump() for c in s.conditions],
                "member_count": s.member_count,
            }
            for s in bundle.segments
        ],
        "merge_tags_in_use": inventory.merge_tags_in_use,
        "consent_buckets": {
            "note": (
                "Counts from the audience export. Every bucket listed here "
                "must receive a consent mapping."
            ),
            "observed": inventory.members_by_bucket,
        },
        "suppression_count": inventory.suppression_count,
    }


def _parse_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        cleaned = cleaned.rsplit("```", 1)[0]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ProposalError(f"proposer returned invalid JSON: {exc}") from exc


def assemble_spec(model_output: dict, source: str, target: str) -> MappingSpec:
    """Validate the model's four lists into a draft MappingSpec. Python owns
    the shell; the model may only propose or flag."""
    try:
        spec = MappingSpec(
            source=source,
            target=target,
            generated_at=datetime.now(UTC),
            field_mappings=model_output.get("field_mappings", []),
            merge_tag_mappings=model_output.get("merge_tag_mappings", []),
            segment_mappings=model_output.get("segment_mappings", []),
            consent_mappings=model_output.get("consent_mappings", []),
            suppressions=SuppressionPolicy(),
        )
    except ValidationError as exc:
        raise ProposalError(f"proposer output failed spec validation: {exc}") from exc
    pre_resolved = [
        m for m in spec.reviewed_mappings()
        if m.status not in (MappingStatus.PROPOSED, MappingStatus.FLAGGED)
    ]
    if pre_resolved:
        raise ProposalError(
            f"proposer emitted {len(pre_resolved)} pre-resolved mapping(s) "
            "(approved/edited); only a human reviewer may resolve a mapping"
        )
    if not spec.reviewed_mappings():
        raise ProposalError("proposer returned no mappings")
    return spec


def request_mapping(payload: dict):
    """The raw model call on a prepared payload, returning the API response.

    Split from `propose` so the Phase 5 eval subject (RC1-548) exercises
    exactly the shipped call - same settings, prompt, limits, and error
    mapping - against its frozen case payloads, while keeping the response's
    usage counts for the run record.
    """
    settings = ProposerSettings()
    if not settings.anthropic_api_key:
        raise ProposalError("ESP_ANTHROPIC_API_KEY is not set (see .env.example)")
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    try:
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": "Draft the mapping spec for this source account:\n"
                    + json.dumps(payload, indent=2),
                }
            ],
        )
    except anthropic.RateLimitError as exc:
        raise ProposalError(f"rate limited: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise ProposalError(f"API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ProposalError(f"connection error: {exc}") from exc
    if response.stop_reason == "refusal":
        raise ProposalError("mapping proposal request was refused")
    if response.stop_reason == "max_tokens":
        raise ProposalError("proposal truncated at max_tokens; raise the limit")
    return response


def response_text(response) -> str:
    text = "".join(block.text for block in response.content if block.type == "text")
    if not text.strip():
        raise ProposalError("empty proposer response")
    return text


def propose(bundle: MailchimpBundle, inventory: Inventory, target: str) -> MappingSpec:
    payload = mapping_payload(bundle, inventory, target)
    response = request_mapping(payload)
    return assemble_spec(_parse_json(response_text(response)), source="mailchimp", target=target)
