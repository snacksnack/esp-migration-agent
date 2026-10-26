"""RC1-546 end-to-end against a real Listmonk (docker-compose.yml).

Opt-in: runs only when ESP_LISTMONK_URL/_USERNAME/_PASSWORD are set in the
PROCESS environment (never read from .env - the default suite must pass with
no configuration). Bring the target up with:

    docker compose up -d
    eval "$(uv run python -m executor.bootstrap --export)"
    uv run pytest -m integration
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from executor.engine import execute
from executor.listmonk import ListmonkClient
from executor.plan import build_plan
from executor.reconcile import reconcile
from ingest.mailchimp import load_bundle
from mapping.gate import load_approved

AUDIENCE = Path("fixtures/mailchimp_export/audience")
SUPPLEMENT = Path("fixtures/api_supplement")
APPROVED = Path("specs/approved/mailchimp_to_listmonk.yaml")

_ENV = ("ESP_LISTMONK_URL", "ESP_LISTMONK_USERNAME", "ESP_LISTMONK_PASSWORD")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not all(os.environ.get(k) for k in _ENV),
        reason="Listmonk not configured in the process environment",
    ),
]


@pytest.fixture(scope="module")
def client():
    return ListmonkClient(
        os.environ["ESP_LISTMONK_URL"],
        os.environ["ESP_LISTMONK_USERNAME"],
        os.environ["ESP_LISTMONK_PASSWORD"],
    )


@pytest.fixture(scope="module")
def migrated(client):
    bundle = load_bundle(AUDIENCE, SUPPLEMENT)
    spec = load_approved(APPROVED)
    plan = build_plan(bundle, spec)
    summary = execute(plan, client)
    return bundle, spec, plan, summary


def test_full_fixture_lands(migrated, client):
    _bundle, _spec, plan, summary = migrated
    total_writes = sum(summary.written.values()) + summary.skipped_unchanged
    assert total_writes == len(plan.all_subscribers) + len(plan.campaigns)
    current = client.current_subscribers(summary.list_id)
    assert len(current) >= len(plan.all_subscribers)


def test_reconciliation_passes_against_real_listmonk(migrated, client):
    bundle, spec, _plan, _summary = migrated
    report = reconcile(bundle, spec, client)
    assert report.suppression.pct == 100.0, report.suppression.violations[:5]
    assert report.passed, [c for c in report.consent_counts if not c.ok]


def test_second_run_converges_on_real_listmonk(migrated, client):
    bundle, spec, _plan, first_summary = migrated
    plan = build_plan(bundle, load_approved(APPROVED))
    summary = execute(plan, client)
    assert summary.written == {}, "second run should write nothing"
    assert summary.skipped_unchanged == sum(first_summary.written.values()) + (
        first_summary.skipped_unchanged
    )
