"""Reconciliation report (RC1-546): did the target land what the plan says?

Checks, all deterministic:
- consent counts: expected (status, subscription) totals vs the target's
- suppression integrity: EVERY suppressed address enumerated (never sampled)
  and required non-subscribable in the target; the report fails below 100%
- segment parity: the target's count for each segment query vs the same
  predicate evaluated in Python over the plan (plus the source count as a
  reference - source parity is not required, the spec may have deliberately
  changed semantics, and the report says which)
- sampled checksums: a deterministic sample of subscribers hashed field by
  field on both sides
- campaign translation: target bodies carry no residual source merge tags,
  and a render comparison - both sides substituted with the same sample
  values - proves the translation preserved the surrounding content

Against a destination that already held data (RC1-574, ADR-0019), the first
run's journaled baseline says what was there: pre-existing members are left
out of membership, and a planned address the destination ALREADY protected
and still protects - status untouched - is an expected deviation (ADR-0013),
reported on its own and not a failure. Without a baseline the destination is
assumed empty, as before. Suppression integrity never consults the baseline.

Usage: uv run python -m executor.reconcile --spec specs/approved/<spec>.yaml

Parallel run (RC1-557): built to be scheduled daily. Exit 0 PASS, 1 FAIL,
2 could not reconcile (gate, ingest, or target error) - a scheduler can tell
a failed check from a broken run. The last line printed is the one-line
verdict, and --history appends one JSONL record per run so the window's
trend is a file, not a memory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from executor.clients import TARGETS, client_for_target
from executor.journal import Baseline, find_baseline
from executor.plan import (
    MERGE_TAG_RE,
    DesiredSubscriber,
    MigrationPlan,
    build_plan,
    existing_record_protected,
)
from executor.queries import QueryNotEvaluatable, evaluate_query
from executor.target import TargetClient, TargetError, TargetSubscriber
from ingest.mailchimp import DEFAULT_ACCOUNT_TZ, IngestError, load_bundle
from ingest.models import MailchimpBundle
from mapping.gate import GateError, load_approved, verify_source
from specs.model import MappingSpec
from telemetry import count, stage

FIXTURE_AUDIENCE = Path("fixtures/mailchimp_export/audience")
FIXTURE_SUPPLEMENT = Path("fixtures/api_supplement")
SAMPLE_TARGET = 50
DEFAULT_JOURNAL = Path("docs/execution_journal.jsonl")


def subscriber_checksum(email: str, name: str, status: str, subscription: str,
                        attribs: dict) -> str:
    canonical = json.dumps(
        {"email": email.lower(), "name": name, "status": status,
         "subscription": subscription, "attribs": {k: str(v) for k, v in attribs.items()}},
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class CountCheck(BaseModel):
    label: str
    expected: int
    actual: int

    @property
    def ok(self) -> bool:
        return self.expected == self.actual


class SegmentCheck(BaseModel):
    name: str
    query: str
    target_count: int
    plan_count: int | None  # None: query not evaluatable in Python
    source_count: int

    @property
    def verified(self) -> bool:
        return self.plan_count is not None

    @property
    def ok(self) -> bool:
        # UNVERIFIED is not a pass: a query Python cannot evaluate failed its
        # check until the query (or the spec) is fixed - a check that did not
        # run must never read as parity (RC1-565).
        return self.verified and self.target_count == self.plan_count


class SuppressionIntegrity(BaseModel):
    total: int
    non_subscribable: int
    violations: list[str]  # emails; must be empty

    @property
    def pct(self) -> float:
        return round(100 * self.non_subscribable / self.total, 2) if self.total else 100.0


class ChecksumCheck(BaseModel):
    sampled: int
    matched: int
    mismatches: list[str]


class MembershipCheck(BaseModel):
    """Full bidirectional email-set comparison. Aggregate counts can mask
    offsetting errors - a wrong address present plus a right one missing nets
    to a matching count - so every address is compared, both ways (RC1-565)."""

    expected: int
    actual: int
    missing: list[str]  # planned, absent from the target list
    unexpected: list[str]  # in the target list, not in the plan, not in the baseline
    preexisting: int = 0  # in the list before the first run, not planned: excluded

    @property
    def ok(self) -> bool:
        return not self.missing and not self.unexpected


class Deviation(BaseModel):
    """A planned address left at the destination's stricter consent: the
    baseline shows it protected before the migration, and it still is."""

    email: str
    planned: str  # "status/subscription"
    before: str
    landed: str


class CampaignCheck(BaseModel):
    name: str
    residual_source_tags: list[str]
    render_matches: bool


class ReconciliationReport(BaseModel):
    list_id: int
    consent_counts: list[CountCheck]
    suppression: SuppressionIntegrity
    membership: MembershipCheck
    segments: list[SegmentCheck]
    checksums: ChecksumCheck
    campaigns: list[CampaignCheck]
    baseline_run: str | None = None
    baseline_note: str = ""  # why no baseline applies (empty when one does)
    deviations: list[Deviation] = []
    # Protected in the baseline, mailable now: ADR-0013 broken, whatever the plan says.
    downgrades: list[str] = []

    @property
    def failing(self) -> list[str]:
        """Names of the checks that failed, in report order."""
        checks = {
            "consent_counts": all(c.ok for c in self.consent_counts),
            "suppression": not self.suppression.violations,
            "downgrades": not self.downgrades,
            "membership": self.membership.ok,
            "segments": all(s.ok for s in self.segments),
            "checksums": not self.checksums.mismatches,
            "campaigns": all(
                not c.residual_source_tags and c.render_matches for c in self.campaigns
            ),
        }
        return [name for name, ok in checks.items() if not ok]

    @property
    def passed(self) -> bool:
        return not self.failing


def _sample(plan: MigrationPlan) -> list[DesiredSubscriber]:
    ordered = sorted(plan.all_subscribers, key=lambda d: d.email.lower())
    step = max(1, len(ordered) // SAMPLE_TARGET)
    return ordered[::step]


SAMPLE_RENDER_VALUES = {
    "first_name": "Maria", "last_name": "Diaz", "state": "NY",
    "zip": "11215", "last_purchase": "2026-01-15",
}
_GO_EXPR_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def _render_source(html: str, tag_map: dict[str, str]) -> str:
    """Substitute source merge tags with sample values, routed through the
    same tag map the plan used, so both renders share one vocabulary."""
    return MERGE_TAG_RE.sub(lambda m: _go_value(tag_map.get(m.group(1), "")), html)


def _go_value(expr: str) -> str:
    inner = _GO_EXPR_RE.match(expr)
    body = inner.group(1) if inner else expr
    if body.startswith(".Subscriber.Attribs."):
        return SAMPLE_RENDER_VALUES.get(body.removeprefix(".Subscriber.Attribs."), "")
    return f"<{body}>"  # system functions (UnsubscribeURL, MessageURL): placeholder


def _render_target(body: str) -> str:
    return _GO_EXPR_RE.sub(lambda m: _go_value(m.group(0)), body)


def _deviation(
    d: DesiredSubscriber, landed: TargetSubscriber, target: str, baseline: Baseline
) -> Deviation | None:
    """Accepted only with evidence: the migration wanted this address
    subscribable, the baseline shows the destination already protected it,
    and it landed protected with its global status as found. A record the
    migration itself made stricter has no baseline evidence and still fails."""
    key = d.email.lower()
    if key in baseline.members:
        before_status, before_subscription = baseline.members[key]
    elif key in baseline.elsewhere:
        before_status, before_subscription = baseline.elsewhere[key], ""
    else:
        return None
    if not (
        d.subscribable
        and existing_record_protected(target, before_status, before_subscription)
        and landed.status == before_status
        and existing_record_protected(target, landed.status, landed.subscription)
    ):
        return None
    return Deviation(
        email=d.email,
        planned=f"{d.status}/{d.subscription}",
        before=f"{before_status}/{before_subscription or '(another list)'}",
        landed=f"{landed.status}/{landed.subscription}",
    )


def build_reconciliation(
    plan: MigrationPlan,
    spec: MappingSpec,
    bundle: MailchimpBundle,
    client: TargetClient,
    list_id: int,
    baseline: Baseline | None = None,
    baseline_note: str = "",
) -> ReconciliationReport:
    current = client.current_subscribers(list_id)

    plan_emails = {d.email.lower() for d in plan.all_subscribers}
    preexisting = (
        {k for k in baseline.members if k not in plan_emails} & current.keys()
        if baseline
        else set()
    )
    membership = MembershipCheck(
        expected=len(plan_emails),
        actual=len(current),
        missing=sorted(plan_emails - current.keys()),
        unexpected=sorted(current.keys() - plan_emails - preexisting),
        preexisting=len(preexisting),
    )

    deviations: dict[str, Deviation] = {}
    if baseline is not None:
        for d in plan.all_subscribers:
            landed = current.get(d.email.lower())
            if landed is None or (landed.status, landed.subscription) == (
                d.status, d.subscription
            ):
                continue
            found = _deviation(d, landed, plan.target, baseline)
            if found is not None:
                deviations[d.email.lower()] = found

    downgrades = []
    if baseline is not None:
        for email, landed in sorted(current.items()):
            if email in baseline.members:
                before = baseline.members[email]
            elif email in baseline.elsewhere:
                before = (baseline.elsewhere[email], "")
            else:
                continue
            if existing_record_protected(plan.target, *before) and not (
                existing_record_protected(plan.target, landed.status, landed.subscription)
            ):
                downgrades.append(landed.email)

    # What should be on the list: the plan, with each accepted deviation as
    # it landed, plus the pre-existing members as they are.
    expected_counts: dict[tuple[str, str], int] = {}
    for d in plan.all_subscribers:
        landed = current.get(d.email.lower())
        key = (
            (landed.status, landed.subscription)
            if d.email.lower() in deviations and landed
            else (d.status, d.subscription)
        )
        expected_counts[key] = expected_counts.get(key, 0) + 1
    actual_counts: dict[tuple[str, str], int] = {}
    for email, sub in current.items():
        if email in preexisting:
            continue
        key = (sub.status, sub.subscription)
        actual_counts[key] = actual_counts.get(key, 0) + 1
    consent_counts = [
        CountCheck(
            label=f"{status}/{subscription}",
            expected=n,
            actual=actual_counts.get((status, subscription), 0),
        )
        for (status, subscription), n in sorted(expected_counts.items())
    ]
    consent_counts.append(
        CountCheck(
            label="total",
            expected=len(plan.all_subscribers),
            actual=len(current) - len(preexisting),
        )
    )

    violations = []
    for d in plan.suppressions:
        landed = current.get(d.email.lower())
        if landed is None or (
            landed.status != "blocklisted" and landed.subscription != "unsubscribed"
        ):
            violations.append(d.email)
    suppression = SuppressionIntegrity(
        total=len(plan.suppressions),
        non_subscribable=len(plan.suppressions) - len(violations),
        violations=violations,
    )

    # The population a segment query should see on this list.
    population = [
        current[d.email.lower()].attribs if d.email.lower() in deviations else d.attribs
        for d in plan.all_subscribers
    ] + [current[k].attribs for k in sorted(preexisting)]
    segments = []
    for seg in plan.segments:
        try:
            plan_count = sum(1 for attribs in population if evaluate_query(seg.query, attribs))
        except QueryNotEvaluatable:
            plan_count = None
        segments.append(
            SegmentCheck(
                name=seg.name,
                query=seg.query,
                target_count=client.count_query(list_id, seg.query),
                plan_count=plan_count,
                source_count=seg.source_member_count,
            )
        )

    sample = _sample(plan)
    mismatches = []
    for d in sample:
        if d.email.lower() in deviations:
            continue  # reported as a deviation, never silently matched
        landed = current.get(d.email.lower())
        expected_sum = subscriber_checksum(
            d.email, d.name, d.status, d.subscription, d.attribs
        )
        actual_sum = (
            subscriber_checksum(
                landed.email,
                landed.name,
                landed.status,
                landed.subscription,
                # checksum only the keys the executor owns (target extras pass)
                {k: landed.attribs.get(k) for k in d.attribs},
            )
            if landed
            else "missing"
        )
        if expected_sum != actual_sum:
            mismatches.append(d.email)
    checksums = ChecksumCheck(
        sampled=len(sample), matched=len(sample) - len(mismatches), mismatches=mismatches
    )

    tag_map = {m.source_tag.strip("*|"): m.target_tag for m in spec.merge_tag_mappings}
    target_campaigns = client.current_campaigns()
    campaign_checks = []
    for planned in plan.campaigns:
        landed_campaign = target_campaigns.get(planned.name)
        body = landed_campaign.body if landed_campaign else ""
        source_html = next(
            c.html for c in bundle.campaigns if c.title == planned.name
        )
        campaign_checks.append(
            CampaignCheck(
                name=planned.name,
                residual_source_tags=sorted(set(MERGE_TAG_RE.findall(body))),
                render_matches=_render_source(source_html, tag_map).strip()
                == _render_target(body).strip(),
            )
        )

    return ReconciliationReport(
        list_id=list_id,
        consent_counts=consent_counts,
        suppression=suppression,
        membership=membership,
        segments=segments,
        checksums=checksums,
        campaigns=campaign_checks,
        baseline_run=baseline.run_id if baseline else None,
        baseline_note=baseline_note,
        deviations=list(deviations.values()),
        downgrades=downgrades,
    )


def reconcile(
    bundle: MailchimpBundle,
    spec: MappingSpec,
    client: TargetClient,
    journal: Path | None = None,
) -> ReconciliationReport:
    plan = build_plan(bundle, spec)
    # Reconciliation reads; it never creates the list it is checking (RC1-564).
    list_id = client.find_list(plan.list_name)
    if list_id is None:
        raise TargetError(
            f"list {plan.list_name!r} not found in the target; nothing to "
            "reconcile - run the executor first"
        )
    if journal is None:
        return build_reconciliation(
            plan, spec, bundle, client, list_id, None, "no journal given; destination assumed empty"
        )
    lookup = find_baseline(journal, client.describe_destination(), plan.list_name)
    baseline, note = lookup.baseline, lookup.reason
    if baseline is not None and baseline.list_id != list_id:
        baseline, note = None, (
            f"baseline {baseline.run_id} was taken of list {baseline.list_id}, not "
            f"{list_id} (recreated since?); destination assumed empty"
        )
    return build_reconciliation(plan, spec, bundle, client, list_id, baseline, note)


def render_markdown(report: ReconciliationReport) -> str:
    sections = [
        "# Reconciliation report",
        f"Overall: **{'PASS' if report.passed else 'FAIL'}**",
        "## Consent counts",
        "| State | Expected | In target | OK |",
        "| --- | ---: | ---: | --- |",
    ]
    sections[-2:] = ["\n".join(
        sections[-2:]
        + [
            f"| {c.label} | {c.expected:,} | {c.actual:,} | {'yes' if c.ok else 'NO'} |"
            for c in report.consent_counts
        ]
    )]
    s = report.suppression
    sections += [
        "## Baseline",
        (
            f"Destination as found by run {report.baseline_run}: "
            f"{report.membership.preexisting:,} pre-existing member(s) outside the plan "
            "are excluded from membership."
            if report.baseline_run
            else f"No baseline: {report.baseline_note}."
        ),
    ]
    sections += [
        "## Suppression integrity (required: 100%)",
        f"{s.non_subscribable:,} of {s.total:,} suppressed addresses are "
        f"non-subscribable in the target ({s.pct}%)."
        + (f" VIOLATIONS: {', '.join(s.violations[:10])}" if s.violations else ""),
        "## Protection kept (required: no downgrades)",
        (
            f"DOWNGRADED: {len(report.downgrades):,} address(es) the destination "
            f"protected before the migration are mailable now: "
            f"{', '.join(report.downgrades[:10])}"
            if report.downgrades
            else (
                "Every address the baseline shows protected is still protected."
                if report.baseline_run
                else "Not checked: no baseline."
            )
        ),
        "## Membership (every address, both directions)",
        f"{report.membership.actual:,} addresses in the target list vs "
        f"{report.membership.expected:,} planned: "
        f"{len(report.membership.missing):,} missing, "
        f"{len(report.membership.unexpected):,} unexpected."
        + (
            f" MISSING: {', '.join(report.membership.missing[:10])}"
            if report.membership.missing
            else ""
        )
        + (
            f" UNEXPECTED: {', '.join(report.membership.unexpected[:10])}"
            if report.membership.unexpected
            else ""
        ),
        "## Expected deviations (kept stricter consent, ADR-0013)",
        (
            "\n".join(
                [
                    f"{len(report.deviations):,} planned address(es) the destination "
                    "already protected stay protected - not failures:",
                    "",
                    "| Address | Planned | Before | Landed |",
                    "| --- | --- | --- | --- |",
                ]
                + [
                    f"| {d.email} | {d.planned} | {d.before} | {d.landed} |"
                    for d in report.deviations[:25]
                ]
                + (
                    [f"\n...and {len(report.deviations) - 25:,} more."]
                    if len(report.deviations) > 25
                    else []
                )
            )
            if report.deviations
            else "None."
        ),
        "## Segment parity",
        "\n".join(
            [
                "| Segment | Target | Plan | Source (ref) | OK |",
                "| --- | ---: | ---: | ---: | --- |",
            ]
            + [
                f"| {seg.name} | {seg.target_count:,} | "
                f"{seg.plan_count if seg.plan_count is not None else 'not evaluated'} | "
                f"{seg.source_count:,} | "
                f"{'yes' if seg.ok else ('UNVERIFIED' if not seg.verified else 'NO')} |"
                for seg in report.segments
            ]
        ),
        "Source counts are references, not requirements: the approved spec may "
        "deliberately change segment semantics; parity is required between the "
        "plan and the target.",
        "## Sampled checksums",
        f"{report.checksums.matched} of {report.checksums.sampled} sampled "
        "subscribers match field-for-field."
        + (
            f" MISMATCHES: {', '.join(report.checksums.mismatches[:10])}"
            if report.checksums.mismatches
            else ""
        ),
        "## Campaign translation",
        "\n".join(
            ["| Campaign | Residual source tags | Render comparison |", "| --- | --- | --- |"]
            + [
                f"| {c.name} | {', '.join(c.residual_source_tags) or 'none'} | "
                f"{'match' if c.render_matches else 'MISMATCH'} |"
                for c in report.campaigns
            ]
        ),
    ]
    return "\n\n".join(sections) + "\n"


def verdict_line(report: ReconciliationReport) -> str:
    if report.passed:
        return "reconcile PASS"
    return f"reconcile FAIL: {', '.join(report.failing)}"


def history_record(report: ReconciliationReport, spec: MappingSpec) -> dict:
    return {
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "passed": report.passed,
        "failing": report.failing,
        "suppression_pct": report.suppression.pct,
        "deviations": len(report.deviations),
        "baseline_run": report.baseline_run,
        "spec_sha256": spec.approval.content_sha256 if spec.approval else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True, help="APPROVED spec yaml")
    parser.add_argument("--audience-dir", type=Path, default=FIXTURE_AUDIENCE)
    parser.add_argument("--supplement-dir", type=Path, default=FIXTURE_SUPPLEMENT)
    parser.add_argument("--account-tz", default=DEFAULT_ACCOUNT_TZ)
    parser.add_argument("--target", choices=TARGETS, default="listmonk")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--journal",
        type=Path,
        default=DEFAULT_JOURNAL,
        help="execution journal holding the destination's baseline (RC1-574)",
    )
    parser.add_argument(
        "--history",
        type=Path,
        default=None,
        help="append one JSONL verdict record per run (parallel-run trend, RC1-557)",
    )
    args = parser.parse_args()
    try:
        return _run(args)
    except (GateError, IngestError, TargetError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("reconcile ERROR: could not run")
        return 2


def _run(args: argparse.Namespace) -> int:
    with stage("reconcile", target=args.target):
        spec = load_approved(args.spec)
        # Same input binding as the executor (RC1-566): reconciling against a
        # different export or adapter than the approval covers is meaningless.
        if args.target != spec.target:
            raise TargetError(
                f"--target {args.target} does not match the approved spec's "
                f"target {spec.target!r}"
            )
        verify_source(spec, args.audience_dir, args.supplement_dir, args.account_tz)
        bundle = load_bundle(args.audience_dir, args.supplement_dir, args.account_tz)
        client = client_for_target(args.target, need_writes=True)
        report = reconcile(bundle, spec, client, args.journal)
    count(
        "reconcile_result",
        tags={"outcome": "pass" if report.passed else "fail", "target": args.target},
    )
    markdown = render_markdown(report)
    if args.out:
        args.out.write_text(markdown)
        print(f"wrote {args.out}")
    else:
        print(markdown)
    if args.history:
        args.history.parent.mkdir(parents=True, exist_ok=True)
        with args.history.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(history_record(report, spec), separators=(",", ":")) + "\n")
    print(verdict_line(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
