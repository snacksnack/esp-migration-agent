"""RC1-573 against a real Listmonk: two audiences of one account, each into
its own list, with the 15 addresses audience A suppressed and audience B
subscribed. The unit suite proves the engine against a fake built from the
probed behavior; this proves the probe still holds on the real API.

Opt-in, same gate as test_integration_listmonk.py (process env only):

    docker compose up -d
    eval "$(uv run python -m executor.bootstrap --export)"
    uv run pytest -m integration
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from executor.engine import execute
from executor.listmonk import ListmonkClient
from executor.plan import build_plan
from ingest.mailchimp import load_bundle
from mapping.gate import approve, load_approved
from seeding.build_corpora import AUDIENCE_A, AUDIENCE_B, SYNTHETIC
from specs.model import MappingSpec

APPROVED = Path("specs/approved/mailchimp_to_listmonk.yaml")
MULTI = SYNTHETIC / "multi_audience"

_ENV = ("ESP_LISTMONK_URL", "ESP_LISTMONK_USERNAME", "ESP_LISTMONK_PASSWORD")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not all(os.environ.get(k) for k in _ENV),
        reason="Listmonk not configured in the process environment",
    ),
]


def spec_for(audience: str) -> MappingSpec:
    data = load_approved(APPROVED).model_dump(mode="json")
    data.update(approval=None, status="draft", source_audience=audience)
    return approve(MappingSpec.model_validate(data), approved_by="integration test")


@pytest.fixture(scope="module")
def migrated():
    client = ListmonkClient(*(os.environ[k] for k in _ENV))
    plan_a = build_plan(
        load_bundle(MULTI / "audience_a", MULTI / "api_supplement"), spec_for(AUDIENCE_A)
    )
    plan_b = build_plan(
        load_bundle(MULTI / "audience_b", MULTI / "api_supplement"), spec_for(AUDIENCE_B)
    )
    execute(plan_a, client)
    summary_b = execute(plan_b, client)  # pre-RC1-573: HTTP 409 on the first shared address
    truth = json.loads((MULTI / "ground_truth.json").read_text())
    return client, plan_a, plan_b, summary_b, truth


def test_second_audience_runs_against_a_shared_account(migrated):
    *_, summary_b, truth = migrated
    assert set(summary_b.conflicts) == set(truth["overlap_consent_conflicts"])


def test_first_audiences_suppressions_survive_on_both_lists(migrated):
    client, plan_a, plan_b, _, truth = migrated
    list_a, list_b = client.find_list(plan_a.list_name), client.find_list(plan_b.list_name)
    assert list_a != list_b
    on_b = client.current_subscribers(list_b)
    found = client.lookup_subscribers(truth["overlap_consent_conflicts"])
    for email in truth["overlap_consent_conflicts"]:
        assert found[email].status == "blocklisted"
        assert sorted(found[email].list_ids) == sorted([list_a, list_b])
        assert on_b[email].subscription == "unsubscribed"


def test_shared_subscribers_keep_the_first_list(migrated):
    client, plan_a, plan_b, _, truth = migrated
    list_a, list_b = client.find_list(plan_a.list_name), client.find_list(plan_b.list_name)
    on_a = client.current_subscribers(list_a)
    for email in truth["overlap_same_consent"]:
        assert on_a[email].subscription == "confirmed"  # B's update did not drop A
        assert list_b in on_a[email].list_ids


def test_second_audience_rerun_converges(migrated):
    client, _, plan_b, _, _ = migrated
    rerun = execute(plan_b, client)
    assert rerun.written == {}
