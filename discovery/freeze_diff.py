"""Freeze-start diff (RC1-557): the fresh freeze-window export against the
one the approved spec was reviewed on.

The approval binds the exact export files (RC1-566), so a fresh export can
never run under the old approval - and should not silently. This command
does the mechanical part of the cutover's step 1: re-run discovery on the
fresh export, print what changed against the reviewed scope, and

- data-only changes (members joined, left, or changed consent): write a
  DRAFT of the same spec bound to the fresh export. The spec owner re-signs
  it with `mapping.review approve` - the human gate stays where it was.
- structural changes (merge-field columns, segments, campaigns, audiences,
  or a spec that no longer builds a plan): write nothing and exit 1; the
  reviewed mappings do not cover the fresh export, so it is re-proposed.

Every number is computed; nothing here calls a model.

    uv run python -m discovery.freeze_diff --spec specs/approved/<spec>.yaml \\
        --reviewed-audience-dir <reviewed> --reviewed-supplement-dir <reviewed> \\
        --audience-dir <fresh> --supplement-dir <fresh>
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from pydantic import BaseModel

from discovery.inventory import build_inventory
from discovery.risks import build_risk_register
from executor.plan import PlanError, build_plan
from ingest.mailchimp import (
    DEFAULT_ACCOUNT_TZ,
    IngestError,
    audiences_in,
    bundle_fingerprint,
    load_bundle,
    scope_to_audience,
)
from ingest.models import ConsentBucket, MailchimpBundle
from mapping.gate import GateError, load_approved, verify_source
from specs.model import MappingSpec, SpecStatus

FIXTURE_AUDIENCE = Path("fixtures/mailchimp_export/audience")
FIXTURE_SUPPLEMENT = Path("fixtures/api_supplement")
SHOWN = 10


class Transition(BaseModel):
    before: str
    after: str
    emails: list[str]


class FreezeDiff(BaseModel):
    reviewed_counts: dict[str, int]
    fresh_counts: dict[str, int]
    added: list[str]  # in the fresh export only
    removed: list[str]  # in the reviewed export only: the executor never deletes them
    transitions: list[Transition]  # consent bucket changes, both exports
    columns_added: list[str]
    columns_removed: list[str]
    segments_added: list[str]
    segments_removed: list[str]
    campaigns_added: list[str]
    campaigns_removed: list[str]
    audiences_added: list[str]
    audiences_removed: list[str]
    risks_added: list[str]
    risks_resolved: list[str]
    plan_error: str  # "" when the approved spec still builds a plan on the fresh export

    @property
    def new_suppressions(self) -> list[str]:
        return sorted(
            e for t in self.transitions if t.before == ConsentBucket.SUBSCRIBED for e in t.emails
        )

    @property
    def resubscribes(self) -> list[str]:
        return sorted(
            e for t in self.transitions if t.after == ConsentBucket.SUBSCRIBED for e in t.emails
        )

    @property
    def structural(self) -> list[str]:
        """What the reviewed mappings do not cover - each one means re-propose."""
        reasons = []
        for label, added, removed in (
            ("merge-field columns", self.columns_added, self.columns_removed),
            ("segments", self.segments_added, self.segments_removed),
            ("campaigns", self.campaigns_added, self.campaigns_removed),
            ("audiences", self.audiences_added, self.audiences_removed),
        ):
            if added or removed:
                reasons.append(label)
        if self.plan_error:
            reasons.append("the approved spec no longer builds a plan")
        return reasons


def _buckets(bundle: MailchimpBundle) -> dict[str, str]:
    return {
        m.email.lower(): str(bucket)
        for bucket, members in bundle.members.items()
        for m in members
    }


def _added_removed(before: set[str], after: set[str]) -> tuple[list[str], list[str]]:
    return sorted(after - before), sorted(before - after)


def build_freeze_diff(
    reviewed: MailchimpBundle, fresh: MailchimpBundle, spec: MappingSpec
) -> FreezeDiff:
    audiences_added, audiences_removed = _added_removed(
        set(audiences_in(reviewed)), set(audiences_in(fresh))
    )
    # Campaign scope is the spec's audience, exactly as the plan sees it.
    reviewed_scoped = scope_to_audience(reviewed, spec.source_audience)
    fresh_scoped = scope_to_audience(fresh, spec.source_audience)

    before, after = _buckets(reviewed), _buckets(fresh)
    moved: dict[tuple[str, str], list[str]] = {}
    for email in sorted(before.keys() & after.keys()):
        if before[email] != after[email]:
            moved.setdefault((before[email], after[email]), []).append(email)
    added, removed = _added_removed(set(before), set(after))

    columns_added, columns_removed = _added_removed(
        set(reviewed.extra_columns), set(fresh.extra_columns)
    )
    segments_added, segments_removed = _added_removed(
        {s.name for s in reviewed.segments}, {s.name for s in fresh.segments}
    )
    campaigns_added, campaigns_removed = _added_removed(
        {c.title for c in reviewed_scoped.campaigns}, {c.title for c in fresh_scoped.campaigns}
    )
    risks_resolved, risks_added = (
        sorted(a - b)
        for a, b in (
            (_risk_codes(reviewed, spec.target), _risk_codes(fresh, spec.target)),
            (_risk_codes(fresh, spec.target), _risk_codes(reviewed, spec.target)),
        )
    )

    try:
        build_plan(fresh, spec)
        plan_error = ""
    except PlanError as exc:
        plan_error = str(exc)

    return FreezeDiff(
        reviewed_counts=dict(sorted(Counter(before.values()).items())),
        fresh_counts=dict(sorted(Counter(after.values()).items())),
        added=added,
        removed=removed,
        transitions=[
            Transition(before=b, after=a, emails=emails)
            for (b, a), emails in sorted(moved.items())
        ],
        columns_added=columns_added,
        columns_removed=columns_removed,
        segments_added=segments_added,
        segments_removed=segments_removed,
        campaigns_added=campaigns_added,
        campaigns_removed=campaigns_removed,
        audiences_added=audiences_added,
        audiences_removed=audiences_removed,
        risks_added=risks_added,
        risks_resolved=risks_resolved,
        plan_error=plan_error,
    )


def _risk_codes(bundle: MailchimpBundle, target: str) -> set[str]:
    return {r.code for r in build_risk_register(bundle, build_inventory(bundle), target)}


def rebind_draft(spec: MappingSpec, fingerprint: str, account_tz: str) -> MappingSpec:
    """The same reviewed mappings, bound to the fresh export and back to
    DRAFT: only `approve` (a named human) can make it runnable again."""
    data = spec.model_dump(mode="json")
    data.update(
        approval=None,
        status=SpecStatus.DRAFT.value,
        source_fingerprint=fingerprint,
        source_timezone=account_tz,
    )
    return MappingSpec.model_validate(data)


def _listed(emails: list[str]) -> str:
    more = f" and {len(emails) - SHOWN:,} more" if len(emails) > SHOWN else ""
    return ", ".join(emails[:SHOWN]) + more


def render_markdown(diff: FreezeDiff) -> str:
    buckets = sorted(set(diff.reviewed_counts) | set(diff.fresh_counts))
    counts = "\n".join(
        ["| Bucket | Reviewed | Fresh | Delta |", "| --- | ---: | ---: | ---: |"]
        + [
            f"| {b} | {diff.reviewed_counts.get(b, 0):,} | {diff.fresh_counts.get(b, 0):,} | "
            f"{diff.fresh_counts.get(b, 0) - diff.reviewed_counts.get(b, 0):+,} |"
            for b in buckets
        ]
    )
    verdict = (
        f"**STRUCTURAL - re-propose**: {', '.join(diff.structural)} changed since review."
        if diff.structural
        else "**DATA ONLY** - the reviewed mappings still cover the fresh export; "
        "re-approve the rebound draft, then run the final delta pass."
    )

    changes = [
        f"- {len(diff.added):,} joined" + (f": {_listed(diff.added)}" if diff.added else ""),
        f"- {len(diff.removed):,} left the export"
        + (
            f": {_listed(diff.removed)} - the executor never deletes, so they stay in "
            "the target; decide with the client"
            if diff.removed
            else ""
        ),
    ] + [
        f"- {t.before} -> {t.after}: {len(t.emails):,} ({_listed(t.emails)})"
        for t in diff.transitions
    ]
    if diff.new_suppressions:
        changes.append(
            f"- {len(diff.new_suppressions):,} newly suppressed: the delta pass writes "
            "them first (phase 1)"
        )
    if diff.resubscribes:
        changes.append(
            f"- {len(diff.resubscribes):,} re-subscribed: where the target already "
            "suppressed them, the delta pass keeps the suppression and reports a "
            "conflict (ADR-0013)"
        )

    structure = []
    for label, added, removed in (
        ("Merge-field columns", diff.columns_added, diff.columns_removed),
        ("Segments", diff.segments_added, diff.segments_removed),
        ("Campaigns", diff.campaigns_added, diff.campaigns_removed),
        ("Audiences", diff.audiences_added, diff.audiences_removed),
    ):
        parts = [f"added {', '.join(added)}"] if added else []
        parts += [f"removed {', '.join(removed)}"] if removed else []
        structure.append(f"- {label}: {'; '.join(parts) or 'unchanged'}")
    structure.append(
        "- Approved spec against the fresh export: "
        + (f"REFUSED - {diff.plan_error}" if diff.plan_error else "builds a plan")
    )

    sections = [
        "# Freeze-start diff",
        verdict,
        "## Members by consent bucket",
        counts,
        "## Membership changes",
        "\n".join(changes),
        "## Structure",
        "\n".join(structure),
        "## Risk register",
        f"- New: {', '.join(diff.risks_added) or 'none'}\n"
        f"- Resolved: {', '.join(diff.risks_resolved) or 'none'}",
    ]
    return "\n\n".join(sections) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True, help="APPROVED spec yaml")
    parser.add_argument("--reviewed-audience-dir", type=Path, default=FIXTURE_AUDIENCE)
    parser.add_argument("--reviewed-supplement-dir", type=Path, default=FIXTURE_SUPPLEMENT)
    parser.add_argument("--audience-dir", type=Path, required=True, help="FRESH export")
    parser.add_argument("--supplement-dir", type=Path, required=True, help="FRESH supplement")
    parser.add_argument("--account-tz", default=DEFAULT_ACCOUNT_TZ)
    parser.add_argument("--out", type=Path, default=None, help="write the markdown here")
    parser.add_argument(
        "--draft-out",
        type=Path,
        default=None,
        help="where the rebound draft goes (default: specs/drafts/<spec>.freeze.yaml)",
    )
    args = parser.parse_args()

    try:
        spec = load_approved(args.spec)
        # The comparison base must be the export the approval actually covers.
        verify_source(
            spec, args.reviewed_audience_dir, args.reviewed_supplement_dir, args.account_tz
        )
        reviewed = load_bundle(
            args.reviewed_audience_dir, args.reviewed_supplement_dir, args.account_tz
        )
        fresh = load_bundle(args.audience_dir, args.supplement_dir, args.account_tz)
    except (GateError, IngestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    diff = build_freeze_diff(reviewed, fresh, spec)
    markdown = render_markdown(diff)
    if args.out:
        args.out.write_text(markdown)
        print(f"wrote {args.out}")
    else:
        print(markdown)
    if diff.structural:
        print(f"no draft written: {', '.join(diff.structural)}; re-propose", file=sys.stderr)
        return 1

    draft = rebind_draft(
        spec, bundle_fingerprint(args.audience_dir, args.supplement_dir), args.account_tz
    )
    out = args.draft_out or Path("specs/drafts") / args.spec.name.replace(
        ".yaml", ".freeze.yaml"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(draft.to_yaml())
    print(
        f"wrote rebound draft {out}; re-approve with: uv run python -m mapping.review "
        f"approve {out} --by \"<spec owner>\""
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
