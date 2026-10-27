"""The self-contained HTML render of the discovery report (RC1-559).

The HTML is a render of the same DiscoveryReport the markdown reads - these
tests pin that it stays self-contained (no external assets), that every
profile aggregate and register entry survives the render, and that dynamic
text is escaped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from pathlib import Path

import pytest

from discovery.html_report import render_html
from discovery.report import build_report
from ingest.mailchimp import load_bundle

AUDIENCE = Path("fixtures/mailchimp_export/audience")
SUPPLEMENT = Path("fixtures/api_supplement")
AS_OF = datetime(2026, 10, 24, tzinfo=UTC)


@pytest.fixture(scope="module")
def report():
    bundle = load_bundle(AUDIENCE, SUPPLEMENT)
    return build_report(bundle, as_of=AS_OF, target="listmonk")


@pytest.fixture(scope="module")
def html(report):
    return render_html(report)


def test_self_contained(html):
    """One file, no network: nothing fetched, no scripts."""
    for marker in ("http://", "https://", "src=", "<script", "@import", "url("):
        assert marker not in html


def test_headline_counts_render(report, html):
    assert f"{report.inventory.members_total:,}" in html
    assert f"{report.inventory.suppression_count:,}" in html


def test_every_profile_aggregate_renders(report, html):
    prof = report.profile
    for bucket, count in prof.consent.items():
        assert bucket in html and f"{count:,}" in html
    for domain in prof.top_domains:
        assert domain in html
    for cohort_count in prof.tenure_cohorts.values():
        assert f"{cohort_count:,}" in html


def test_risk_register_renders_with_severity_badges(report, html):
    assert len(report.risks) > 0
    for risk in report.risks:
        assert escape(risk.title) in html
    # Severity is never color alone: the glyph and the text label both render.
    assert '<span class="glyph"' in html
    assert ">high</span>" in html


def test_proposal_workstreams_render(report, html):
    for ws in report.proposal.workstreams:
        assert ws.name in html


def test_narration_section_only_when_present(report, html):
    assert "Who this audience is" not in html
    narrated = report.model_copy(update={"narration": "A <b>narrated</b> summary."})
    narrated_html = render_html(narrated)
    assert "Who this audience is" in narrated_html
    # Dynamic text is escaped, not interpreted.
    assert "<b>narrated</b>" not in narrated_html
    assert "&lt;b&gt;narrated&lt;/b&gt;" in narrated_html
