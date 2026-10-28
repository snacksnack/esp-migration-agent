"""Review-gate CLI (RC1-545): propose, inspect, and approve mapping specs.

The review surface is the YAML document itself (ADR-0003); this CLI wraps it:

    uv run python -m mapping.review propose [--target listmonk] [--out PATH]
    uv run python -m mapping.review status SPEC.yaml
    uv run python -m mapping.review approve SPEC.yaml --by "Reid Collins" [--out PATH]

propose needs ESP_ANTHROPIC_API_KEY. status renders the reviewer's worklist:
counts by review status and every open question. approve refuses a spec with
unresolved mappings, stamps who/when/sha256, and writes the approved file -
the only file downstream stages will read (mapping.gate.load_approved).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from discovery.inventory import build_inventory
from ingest.mailchimp import (
    DEFAULT_ACCOUNT_TZ,
    IngestError,
    bundle_fingerprint,
    load_bundle,
    resolve_audience,
    scope_to_audience,
)
from mapping.gate import GateError, approve, load_spec, mapping_label
from mapping.proposer import ProposalError, propose
from specs.model import MappingSpec, MappingStatus
from telemetry import count, enable_llmobs, stage, workflow

FIXTURE_AUDIENCE = Path("fixtures/mailchimp_export/audience")
FIXTURE_SUPPLEMENT = Path("fixtures/api_supplement")
DRAFTS_DIR = Path("specs/drafts")


def render_status(spec: MappingSpec) -> str:
    counts = Counter(m.status.value for m in spec.reviewed_mappings())
    lines = [
        f"{spec.source} -> {spec.target} | spec status: {spec.status.value}",
        f"mappings: {len(spec.reviewed_mappings())} reviewed "
        f"({', '.join(f'{k}: {v}' for k, v in sorted(counts.items()))}), "
        f"{len(spec.merge_tag_mappings)} merge tags (mechanical)",
    ]
    flagged = [m for m in spec.reviewed_mappings() if m.status is MappingStatus.FLAGGED]
    if flagged:
        lines.append(f"\nopen questions ({len(flagged)}):")
        for i, m in enumerate(flagged, 1):
            lines.append(f"  {i}. [{mapping_label(m)}] {m.open_question}")
    proposed = [m for m in spec.reviewed_mappings() if m.status is MappingStatus.PROPOSED]
    if proposed:
        lines.append(
            f"\nawaiting review ({len(proposed)}): "
            + ", ".join(mapping_label(m) for m in proposed)
        )
    if spec.approval:
        lines.append(
            f"\napproved by {spec.approval.approved_by} at "
            f"{spec.approval.approved_at.isoformat()}"
        )
    if not flagged and not proposed and not spec.approval:
        lines.append("\nall mappings resolved; ready to approve")
    return "\n".join(lines)


def cmd_propose(args: argparse.Namespace) -> int:
    enable_llmobs()
    with stage("propose"), workflow("mapping.propose"):
        bundle = load_bundle(args.audience_dir, args.supplement_dir, args.account_tz)
        # One audience per proposal (RC1-573): the proposer sees only this
        # audience's campaigns and merge tags.
        audience = resolve_audience(bundle, args.audience)
        bundle = scope_to_audience(bundle, audience)
        spec = propose(bundle, build_inventory(bundle), target=args.target)
    # Stamp the source identity the review (and later the approval) covers
    # (RC1-566/RC1-569/RC1-573). Python's stamp, never the model's.
    spec = spec.model_copy(
        update={
            "source_fingerprint": bundle_fingerprint(
                args.audience_dir, args.supplement_dir
            ),
            "source_timezone": args.account_tz,
            "source_audience": audience,
        }
    )
    out: Path = args.out or DRAFTS_DIR / f"{spec.source}_to_{spec.target}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(spec.to_yaml())
    print(f"wrote draft {out}\n")
    print(render_status(spec))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(render_status(load_spec(args.spec)))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    try:
        approved = approve(load_spec(args.spec), approved_by=args.by)
    except GateError:
        count("gate_approval", tags={"outcome": "refused"})
        raise
    count("gate_approval", tags={"outcome": "approved"})
    out: Path = args.out or args.spec.with_name(
        args.spec.name.replace(".yaml", ".approved.yaml")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(approved.to_yaml())
    print(f"approved by {args.by}; wrote {out}")
    print(f"content sha256: {approved.approval.content_sha256}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("propose", help="generate a draft spec from the source bundle")
    p.add_argument("--audience-dir", type=Path, default=FIXTURE_AUDIENCE)
    p.add_argument("--supplement-dir", type=Path, default=FIXTURE_SUPPLEMENT)
    p.add_argument("--account-tz", default=DEFAULT_ACCOUNT_TZ)
    p.add_argument("--target", default="listmonk")
    p.add_argument(
        "--audience",
        default=None,
        help="which audience this export is; required when the supplement spans several",
    )
    p.add_argument("--out", type=Path, default=None)
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("status", help="show review status and open questions")
    p.add_argument("spec", type=Path)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("approve", help="stamp approval on a fully reviewed spec")
    p.add_argument("spec", type=Path)
    p.add_argument("--by", required=True, help="reviewer name recorded in the approval")
    p.add_argument("--out", type=Path, default=None)
    p.set_defaults(func=cmd_approve)

    args = parser.parse_args()
    try:
        return args.func(args)
    except (GateError, ProposalError, IngestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
