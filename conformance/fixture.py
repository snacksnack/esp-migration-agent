"""Per-run conformance fixture (RC1-641, ADR-0029).

One synthetic export per run, namespaced so runs never share a destination
record: every address is `cik-<run>-NNNN@<domain>`, the audience (and so the
destination list and the one campaign) is `cik-conformance <run>`. A rerun
therefore cannot pass by converging onto an earlier run's contacts.

The draft spec is the repo's reviewed Mailchimp spec, bound to this export
(fingerprint, timezone, audience) and to the Brevo target, with its segment
mappings dropped (their ground truth belongs to the genuine fixture, not to
these records). It is written as a DRAFT: the caller still has to take it
through mapping.gate.approve, and the executor still reads it only through
load_approved. Nothing here approves anything.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from executor.plan import destination_list_name
from ingest.mailchimp import DEFAULT_ACCOUNT_TZ, bundle_fingerprint
from mapping.gate import load_spec
from seeding.bundle_writer import make_campaign, write_audience_csvs, write_supplement
from seeding.generator import Subscriber, generate
from specs.model import MappingSpec, SpecStatus

FIXTURE_SCHEMA = "esp.conformance-fixture/1"
REVIEWED_SPEC = Path("specs/drafts/mailchimp_to_listmonk.resolved.yaml")
TARGET = "brevo"
DEFAULT_COUNT = 40
DEFAULT_DOMAIN = "example.com"
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{3,39}$")

# The first records are pinned to these statuses so every consent bucket the
# spec maps, and the suppression set, is exercised whatever the seed draws.
# "pending" is pinned too: the export omits it, and the run must not count it.
PINNED_STATUSES = ("subscribed", "unsubscribed", "unsubscribed", "transactional", "pending")

CAMPAIGN_HTML = (
    "<p>Hi *|FNAME|*,</p><p>Install-kit conformance fixture. Never sent.</p>"
    '<p><a href="*|UNSUB|*">Unsubscribe</a></p>'
)

# generator status -> export bucket the spec's consent mappings name
BUCKET_OF = {
    "subscribed": "subscribed",
    "unsubscribed": "unsubscribed",
    "transactional": "nonsubscribed",
}


class FixtureError(ValueError):
    pass


def namespace(run_id: str) -> str:
    return f"cik-{run_id}-"


def audience_name(run_id: str) -> str:
    return f"cik-conformance {run_id}"


def email_pattern(run_id: str, domain: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(namespace(run_id))}\d{{4}}@{re.escape(domain)}$")


def _seed(run_id: str) -> int:
    return int.from_bytes(hashlib.sha256(run_id.encode()).digest()[:4], "big")


def _namespaced(subs: list[Subscriber], run_id: str, domain: str) -> list[Subscriber]:
    out = []
    for i, sub in enumerate(subs):
        update: dict = {"email": f"{namespace(run_id)}{i:04d}@{domain}"}
        if i < len(PINNED_STATUSES):
            update["status"] = PINNED_STATUSES[i]
        out.append(sub.model_copy(update=update))
    return out


def build(
    run_id: str,
    out: Path,
    *,
    count: int = DEFAULT_COUNT,
    domain: str = DEFAULT_DOMAIN,
    reviewed_spec: Path = REVIEWED_SPEC,
    now: datetime | None = None,
) -> dict:
    """Write audience/, api_supplement/, spec.draft.yaml and expected.json
    under `out` (which must not exist yet); return the expected record."""
    if not RUN_ID.match(run_id):
        raise FixtureError(f"run id {run_id!r} must match {RUN_ID.pattern}")
    if count < len(PINNED_STATUSES):
        raise FixtureError(f"count must be at least {len(PINNED_STATUSES)}")
    if out.exists():
        raise FixtureError(f"{out} exists; a fixture is written once per run")

    subs, _truth = generate(count, seed=_seed(run_id))
    subs = _namespaced(subs, run_id, domain)
    audience = audience_name(run_id)

    audience_dir, supplement_dir = out / "audience", out / "api_supplement"
    write_audience_csvs(subs, audience_dir, export_id=f"cik{_seed(run_id)}")
    exported = [s for s in subs if s.status in BUCKET_OF]
    write_supplement(
        supplement_dir,
        campaigns=[
            make_campaign(
                f"cik{_seed(run_id)}", audience, "Conformance fixture", CAMPAIGN_HTML,
                list_id=f"cik{_seed(run_id)}", recipient_count=len(exported),
                list_name=audience,
            )
        ],
    )

    reviewed = load_spec(reviewed_spec)
    draft = MappingSpec.model_validate(
        {
            **reviewed.model_dump(mode="json", exclude={"approval"}),
            "target": TARGET,
            "status": SpecStatus.DRAFT.value,
            "segment_mappings": [],
            "source_fingerprint": bundle_fingerprint(audience_dir, supplement_dir),
            "source_timezone": DEFAULT_ACCOUNT_TZ,
            "source_audience": audience,
            "generated_at": (now or datetime.now(UTC)).isoformat(),
            "approval": None,
        }
    )
    (out / "spec.draft.yaml").write_text(draft.to_yaml())

    buckets: dict[str, int] = {}
    for s in exported:
        buckets[BUCKET_OF[s.status]] = buckets.get(BUCKET_OF[s.status], 0) + 1
    suppressed = set(draft.suppressions.source_statuses)
    expected = {
        "schema": FIXTURE_SCHEMA,
        "run_id": run_id,
        "target": TARGET,
        "namespace": namespace(run_id),
        "domain": domain,
        "audience": audience,
        "list_name": destination_list_name(draft),
        "campaign_name": audience,
        "exported_by_bucket": dict(sorted(buckets.items())),
        "exported_total": len(exported),
        "not_exported": sum(1 for s in subs if s.status not in BUCKET_OF),
        "emails": sorted(s.email for s in exported),
        "suppressed_emails": sorted(
            s.email for s in exported if BUCKET_OF[s.status] in suppressed
        ),
        "source_fingerprint": draft.source_fingerprint,
    }
    (out / "expected.json").write_text(json.dumps(expected, indent=1, sort_keys=True) + "\n")
    return expected
