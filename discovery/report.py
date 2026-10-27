"""Assemble and render the discovery report (RC1-544).

The report tells the customer what they have (inventory), who their audience
is (profile), what will hurt (risk register), and what the engagement costs
(proposal) - every number deterministic, with optional LLM narration on top.

Usage:
    uv run python -m discovery.report [--narrate] [--out report.md] [--html report.html]

Defaults point at the committed fixture so the pipeline demos offline.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from discovery.html_report import render_html
from discovery.inventory import Inventory, build_inventory
from discovery.narrator import narrate
from discovery.profile import AudienceProfile, build_profile
from discovery.proposal import Proposal, build_proposal
from discovery.risks import Risk, build_risk_register, sort_risks
from ingest.mailchimp import DEFAULT_ACCOUNT_TZ, load_bundle
from ingest.models import MailchimpBundle
from mapping.gate import load_spec
from mapping.risks import spec_risks
from telemetry import enable_llmobs, stage, workflow

FIXTURE_AUDIENCE = Path("fixtures/mailchimp_export/audience")
FIXTURE_SUPPLEMENT = Path("fixtures/api_supplement")


class DiscoveryReport(BaseModel):
    source: str
    target: str
    as_of: datetime
    inventory: Inventory
    profile: AudienceProfile
    risks: list[Risk]
    proposal: Proposal
    ramp_recommendation: str
    narration: str | None = None


def ramp_recommendation(profile: AudienceProfile) -> str:
    """Stretch: warm-up ramp guidance from the engagement distribution.

    Standard practice is to ramp the new sending domain with the most-engaged
    cohorts first. When the source carries no engagement signal, the honest
    recommendation is to say so - not to invent cohorts."""
    if len(profile.member_rating) == 1:
        return (
            "The source account has no usable engagement signal (uniform member "
            "rating), so an engagement-cohort ramp cannot be derived from this "
            "export. Recommend ramping by signup recency instead (newest cohorts "
            "first) and tightening once the target accumulates its own opens."
        )
    return (
        "Ramp the target domain with the highest member-rating cohorts first, "
        "expanding to lower-rated cohorts as bounce and complaint rates hold."
    )


def build_report(
    bundle: MailchimpBundle,
    as_of: datetime,
    target: str,
    narration: str | None = None,
) -> DiscoveryReport:
    inventory = build_inventory(bundle)
    profile = build_profile(bundle, as_of=as_of)
    risks = build_risk_register(bundle, inventory, target=target)
    return DiscoveryReport(
        source="mailchimp",
        target=target,
        as_of=as_of,
        inventory=inventory,
        profile=profile,
        risks=risks,
        proposal=build_proposal(inventory, risks),
        ramp_recommendation=ramp_recommendation(profile),
        narration=narration,
    )


def _dict_rows(d: dict, headers: tuple[str, str]) -> list[str]:
    lines = [f"| {headers[0]} | {headers[1]} |", "| --- | ---: |"]
    lines += [f"| {k} | {v:,} |" for k, v in d.items()]
    return lines


def render_markdown(report: DiscoveryReport) -> str:
    inv, prof = report.inventory, report.profile
    sections: list[str] = [
        f"# Discovery report: {report.source} -> {report.target}",
        f"Audience data as of {report.as_of.date().isoformat()}. Every count in this "
        "report is computed deterministically from the export bundle; the narrated "
        "summary (when present) cites only these computed numbers.",
    ]

    if report.narration:
        sections += ["## Who this audience is", report.narration]

    sections += [
        "## Inventory",
        "\n".join(
            [
                f"- **Lists:** {inv.lists}",
                f"- **Members in export:** {inv.members_total:,} "
                f"({', '.join(f'{k}: {v:,}' for k, v in inv.members_by_bucket.items())})",
                f"- **Suppression list (migrates first):** {inv.suppression_count:,}",
                f"- **Segments:** {len(inv.segments)} "
                f"({', '.join(s.name for s in inv.segments)})",
                f"- **Templates:** {len(inv.templates)} ({', '.join(inv.templates)})",
                f"- **Campaigns:** "
                f"{', '.join(f'{k}: {v}' for k, v in inv.campaigns_by_status.items())}",
                f"- **Merge tags in use:** {', '.join(inv.merge_tags_in_use)}",
            ]
        ),
        "### Observed merge-field formats",
    ]
    for field, formats in inv.merge_field_formats.items():
        sections.append(f"**{field}**")
        sections.append("\n".join(_dict_rows(formats, ("Format", "Rows"))))

    sections += [
        "## Audience profile",
        "Aggregates only; no record-level data appears in this report.",
        "### Consent mix",
        "\n".join(_dict_rows(prof.consent, ("Status", "Members"))),
        "### Email domains",
        "\n".join(_dict_rows(prof.domain_class, ("Class", "Members"))),
        "Top domains:",
        "\n".join(_dict_rows(prof.top_domains, ("Domain", "Members"))),
        "### Geography (top 10 states)",
        "\n".join(_dict_rows(dict(list(prof.states.items())[:10]), ("State", "Members"))),
        "### Signup tenure",
        "\n".join(_dict_rows(prof.tenure_cohorts, ("Cohort", "Members"))),
        "### Engagement (member rating)",
        "\n".join(
            _dict_rows({str(k): v for k, v in prof.member_rating.items()}, ("Rating", "Members"))
        ),
    ]

    sections += ["## Risk register"]
    risk_lines = ["| Severity | Risk | Affected | Detail |", "| --- | --- | ---: | --- |"]
    for r in report.risks:
        affected = f"{r.count:,}" if r.count is not None else "-"
        risk_lines.append(f"| {r.severity} | {r.title} | {affected} | {r.detail} |")
    sections.append("\n".join(risk_lines))

    prop = report.proposal
    sections += ["## Scope and estimate proposal"]
    ws_lines = ["| Workstream | Driver | Units | Est. hours |", "| --- | --- | ---: | ---: |"]
    for w in prop.workstreams:
        ws_lines.append(f"| {w.name} | {w.driver} | {w.units:,} | {w.est_hours} |")
    ws_lines.append(f"| **Total** | | | **{prop.total_hours}** |")
    sections.append("\n".join(ws_lines))
    sections.append("### Assumptions")
    sections.append("\n".join(f"- {a}" for a in prop.assumptions))
    sections.append("### Out of scope")
    sections.append("\n".join(f"- {o}" for o in prop.out_of_scope))

    sections += ["## Send-ramp recommendation", report.ramp_recommendation]
    return "\n\n".join(sections) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audience-dir", type=Path, default=FIXTURE_AUDIENCE)
    parser.add_argument("--supplement-dir", type=Path, default=FIXTURE_SUPPLEMENT)
    parser.add_argument(
        "--as-of",
        default="2026-10-24",
        help="anchor date for tenure cohorts (YYYY-MM-DD, interpreted as UTC midnight)",
    )
    parser.add_argument("--target", default="listmonk")
    parser.add_argument(
        "--account-tz",
        default=DEFAULT_ACCOUNT_TZ,
        help="source account timezone; Mailchimp renders export timestamps in it",
    )
    parser.add_argument(
        "--narrate",
        action="store_true",
        help="add the LLM-narrated summary (needs ESP_ANTHROPIC_API_KEY)",
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=None,
        help="mapping spec (RC1-545) whose open questions join the risk register",
    )
    parser.add_argument("--out", type=Path, default=None, help="write markdown here")
    parser.add_argument(
        "--html",
        type=Path,
        default=None,
        help="also write the self-contained HTML render here (RC1-559)",
    )
    args = parser.parse_args()

    as_of = datetime.strptime(args.as_of, "%Y-%m-%d").replace(tzinfo=UTC)
    with stage("discovery"):
        bundle = load_bundle(args.audience_dir, args.supplement_dir, args.account_tz)
        report = build_report(bundle, as_of=as_of, target=args.target)
        if args.spec:
            # Flagged-mapping open questions join the same severity-ranked log.
            # Merged after build_report so the effort model stays driven by
            # discovery counts alone.
            report.risks = sort_risks([*report.risks, *spec_risks(load_spec(args.spec))])
        if args.narrate:
            enable_llmobs()
            with workflow("discovery.report"):
                report.narration = narrate(report.profile, report.inventory, report.risks)
    markdown = render_markdown(report)
    if args.html:
        args.html.write_text(render_html(report))
        print(f"wrote {args.html}")
    if args.out:
        args.out.write_text(markdown)
        print(f"wrote {args.out}")
    elif not args.html:
        print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
