"""RC1-547 end-to-end against a real Brevo free account.

Opt-in: runs only when ESP_BREVO_API_KEY is set in the PROCESS environment
(never read from .env). Not in CI: the live Brevo account is a demo asset
with real rate limits, exercised manually:

    ESP_BREVO_API_KEY=... uv run pytest -m integration tests/test_integration_brevo.py

Demo safety holds throughout: synthetic subscribers, draft campaigns only,
nothing is ever sent, suppressed contacts land emailBlacklisted.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from executor.brevo import BrevoClient
from executor.engine import execute
from executor.plan import build_plan
from executor.reconcile import reconcile
from ingest.mailchimp import load_bundle
from mapping.gate import load_approved

AUDIENCE = Path("fixtures/mailchimp_export/audience")
SUPPLEMENT = Path("fixtures/api_supplement")
APPROVED = Path("specs/approved/mailchimp_to_listmonk.yaml")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ESP_BREVO_API_KEY"),
        reason="Brevo not configured in the process environment",
    ),
]


@pytest.fixture(scope="module")
def client():
    return BrevoClient(os.environ["ESP_BREVO_API_KEY"])


@pytest.fixture(scope="module")
def migrated(client):
    bundle = load_bundle(AUDIENCE, SUPPLEMENT)
    spec = load_approved(APPROVED)
    plan = build_plan(bundle, spec)
    summary = execute(plan, client)
    return bundle, spec, plan, summary


def test_same_approved_spec_lands_on_brevo(migrated, client):
    _bundle, _spec, plan, summary = migrated
    total = sum(summary.written.values()) + summary.skipped_unchanged
    assert total == len(plan.all_subscribers) + len(plan.campaigns)


def test_reconciliation_passes_on_brevo(migrated, client):
    bundle, spec, _plan, _summary = migrated
    report = reconcile(bundle, spec, client)
    assert report.suppression.pct == 100.0, report.suppression.violations[:5]
    assert report.passed, [c for c in report.consent_counts if not c.ok]


def test_second_run_converges_on_brevo(migrated, client):
    bundle, spec, _plan, _summary = migrated
    plan = build_plan(bundle, load_approved(APPROVED))
    summary = execute(plan, client)
    assert summary.written == {}, "second run should write nothing"
