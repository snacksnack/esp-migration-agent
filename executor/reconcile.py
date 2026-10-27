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

Usage: uv run python -m executor.reconcile --spec specs/approved/<spec>.yaml
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from pydantic import BaseModel

from executor.clients import TARGETS, client_for_target
from executor.plan import MERGE_TAG_RE, DesiredSubscriber, MigrationPlan, build_plan
from executor.queries import QueryNotEvaluatable, evaluate_query
from executor.target import TargetClient, TargetError
from ingest.mailchimp import DEFAULT_ACCOUNT_TZ, load_bundle
from ingest.models import MailchimpBundle
from mapping.gate import load_approved
from specs.model import MappingSpec
from telemetry import count, stage

FIXTURE_AUDIENCE = Path("fixtures/mailchimp_export/audience")
FIXTURE_SUPPLEMENT = Path("fixtures/api_supplement")
SAMPLE_TARGET = 50



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
    unexpected: list[str]  # in the target list, not in the plan

    @property
    def ok(self) -> bool:
        return not self.missing and not self.unexpected


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

    @property
    def passed(self) -> bool:
        return (
            all(c.ok for c in self.consent_counts)
            and not self.suppression.violations
            and self.membership.ok
            and all(s.ok for s in self.segments)
            and not self.checksums.mismatches
            and all(not c.residual_source_tags and c.render_matches for c in self.campaigns)
        )


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


def build_reconciliation(
    plan: MigrationPlan,
    spec: MappingSpec,
    bundle: MailchimpBundle,
    client: TargetClient,
    list_id: int,
) -> ReconciliationReport:
    current = client.current_subscribers(list_id)

    plan_emails = {d.email.lower() for d in plan.all_subscribers}
    membership = MembershipCheck(
        expected=len(plan_emails),
        actual=len(current),
        missing=sorted(plan_emails - current.keys()),
        unexpected=sorted(current.keys() - plan_emails),
    )

    expected_counts: dict[tuple[str, str], int] = {}
    for d in plan.all_subscribers:
        key = (d.status, d.subscription)
        expected_counts[key] = expected_counts.get(key, 0) + 1
    actual_counts: dict[tuple[str, str], int] = {}
    for sub in current.values():
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
        CountCheck(label="total", expected=len(plan.all_subscribers), actual=len(current))
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

    segments = []
    for seg in plan.segments:
        try:
            plan_count = sum(
                1 for d in plan.all_subscribers if evaluate_query(seg.query, d.attribs)
            )
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
    )


def reconcile(
    bundle: MailchimpBundle, spec: MappingSpec, client: TargetClient
) -> ReconciliationReport:
    plan = build_plan(bundle, spec)
    # Reconciliation reads; it never creates the list it is checking (RC1-564).
    list_id = client.find_list(plan.list_name)
    if list_id is None:
        raise TargetError(
            f"list {plan.list_name!r} not found in the target; nothing to "
            "reconcile - run the executor first"
        )
    return build_reconciliation(plan, spec, bundle, client, list_id)


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
        "## Suppression integrity (required: 100%)",
        f"{s.non_subscribable:,} of {s.total:,} suppressed addresses are "
        f"non-subscribable in the target ({s.pct}%)."
        + (f" VIOLATIONS: {', '.join(s.violations[:10])}" if s.violations else ""),
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True, help="APPROVED spec yaml")
    parser.add_argument("--audience-dir", type=Path, default=FIXTURE_AUDIENCE)
    parser.add_argument("--supplement-dir", type=Path, default=FIXTURE_SUPPLEMENT)
    parser.add_argument("--account-tz", default=DEFAULT_ACCOUNT_TZ)
    parser.add_argument("--target", choices=TARGETS, default="listmonk")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    with stage("reconcile", target=args.target):
        spec = load_approved(args.spec)
        bundle = load_bundle(args.audience_dir, args.supplement_dir, args.account_tz)
        client = client_for_target(args.target, need_writes=True)
        report = reconcile(bundle, spec, client)
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
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
