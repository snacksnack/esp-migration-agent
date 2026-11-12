"""RC1-641 (ADR-0029): conformance fixtures, the Brevo consent dialect, and
the namespace-fenced cleanup. No network: FakeTarget and httpx.MockTransport."""

from __future__ import annotations

import json
import sys

import httpx
import pytest

from conformance import __main__ as conformance_cli
from conformance.cleanup import CleanupRefused, cleanup
from conformance.fixture import PINNED_STATUSES, FixtureError, build, email_pattern
from executor import reconcile as reconcile_module
from executor import run as run_module
from executor.brevo import BREVO_BLACKLISTED, CANONICAL_STATES, BrevoClient
from executor.engine import execute
from executor.plan import CANONICAL_DIALECT, CONSENT_DIALECTS, build_plan
from executor.target import TargetSubscriber
from ingest.mailchimp import load_bundle
from mapping.gate import GateError, approve, load_approved, load_spec
from tests.fake_target import FakeTarget


@pytest.fixture
def fixture_dir(tmp_path):
    out = tmp_path / "run"
    expected = build("unit01", out)
    return out, expected


def _approved(out, tmp_path):
    spec = approve(load_spec(out / "spec.draft.yaml"), approved_by="unit test")
    path = tmp_path / "approved.yaml"
    path.write_text(spec.to_yaml())
    return path


# --- fixture -------------------------------------------------------------------


def test_every_address_is_in_the_run_namespace(fixture_dir):
    _out, expected = fixture_dir
    pattern = email_pattern("unit01", "example.com")
    assert expected["emails"] and all(pattern.match(e) for e in expected["emails"])
    assert expected["list_name"].startswith("cik-conformance unit01")


def test_pinned_statuses_cover_every_bucket_and_suppression(fixture_dir):
    _out, expected = fixture_dir
    assert set(expected["exported_by_bucket"]) == {"subscribed", "unsubscribed", "nonsubscribed"}
    assert len(expected["suppressed_emails"]) >= PINNED_STATUSES.count("unsubscribed")
    assert expected["not_exported"] >= 1  # pending is never in an export


def test_same_run_id_same_export_different_run_id_different(tmp_path):
    a = build("same01", tmp_path / "a")
    b = build("same01", tmp_path / "b")
    c = build("other1", tmp_path / "c")
    assert a["source_fingerprint"] == b["source_fingerprint"]
    assert a["source_fingerprint"] != c["source_fingerprint"]
    assert not set(a["emails"]) & set(c["emails"])


@pytest.mark.parametrize("run_id", ["UPPER1", "ab", "has space", "../escape"])
def test_bad_run_ids_are_refused(tmp_path, run_id):
    with pytest.raises(FixtureError):
        build(run_id, tmp_path / "x")


def test_a_fixture_is_written_once(fixture_dir):
    out, _expected = fixture_dir
    with pytest.raises(FixtureError, match="exists"):
        build("unit01", out)


# --- the gate still holds -------------------------------------------------------


def test_the_draft_never_reaches_the_executor(fixture_dir):
    out, _expected = fixture_dir
    with pytest.raises(GateError, match="not approved"):
        load_approved(out / "spec.draft.yaml")


def test_approved_fixture_plans_for_brevo_and_keeps_the_invariant(fixture_dir, tmp_path):
    out, expected = fixture_dir
    spec = load_approved(_approved(out, tmp_path))
    plan = build_plan(load_bundle(out / "audience", out / "api_supplement"), spec)
    assert plan.target == "brevo"
    assert plan.list_name == expected["list_name"]
    assert sorted(d.email for d in plan.suppressions) == expected["suppressed_emails"]
    assert all(not d.subscribable for d in plan.suppressions)
    assert len(plan.all_subscribers) == expected["exported_total"]


def test_brevo_cli_runs_end_to_end_against_a_fake(fixture_dir, tmp_path, monkeypatch, capsys):
    """`--target brevo` was refused for every spec before ADR-0029."""
    out, expected = fixture_dir
    approved = _approved(out, tmp_path)
    fake = FakeTarget()
    monkeypatch.setattr(run_module, "client_for_target", lambda target, need_writes: fake)
    monkeypatch.setattr(reconcile_module, "client_for_target", lambda target, need_writes: fake)
    common = ["--spec", str(approved), "--audience-dir", str(out / "audience"),
              "--supplement-dir", str(out / "api_supplement"), "--target", "brevo"]
    journal = tmp_path / "journal.jsonl"
    monkeypatch.setattr(sys, "argv", ["executor.run", *common, "--execute",
                                      "--verify-convergence", "--journal", str(journal)])
    assert run_module.main() == 0
    assert len(fake.subscribers) == expected["exported_total"]
    monkeypatch.setattr(sys, "argv", ["executor.reconcile", *common, "--journal", str(journal)])
    assert reconcile_module.main() == 0
    assert capsys.readouterr().out.strip().endswith("reconcile PASS")


def test_brevo_dialect_is_the_canonical_one_the_adapter_maps():
    assert CONSENT_DIALECTS["brevo"] is CANONICAL_DIALECT
    assert {k: (s.status, s.subscription) for k, s in CANONICAL_DIALECT.items()} == CANONICAL_STATES
    assert set(BREVO_BLACKLISTED) == set(CANONICAL_DIALECT)
    # every non-subscribable label lands blacklisted on Brevo
    assert all(BREVO_BLACKLISTED[k] for k, s in CANONICAL_DIALECT.items() if not s.subscribable)


# --- cleanup --------------------------------------------------------------------


class CleanableTarget(FakeTarget):
    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[str] = []

    def find_campaign_id(self, name: str) -> int | None:
        return 7 if name in self.campaigns else None

    def delete_contact(self, contact_id: int) -> None:
        for email, sub in list(self.subscribers.items()):
            if sub.id == contact_id:
                del self.subscribers[email]
                self.deleted.append(email)

    def delete_campaign(self, campaign_id: int) -> None:
        self.campaigns.clear()
        self.deleted.append("campaign")

    def delete_list(self, list_id: int) -> None:
        self.lists = {k: v for k, v in self.lists.items() if v != list_id}
        self.deleted.append("list")


def _migrated(out, tmp_path):
    spec = load_approved(_approved(out, tmp_path))
    plan = build_plan(load_bundle(out / "audience", out / "api_supplement"), spec)
    target = CleanableTarget()
    execute(plan, target)
    return target


def test_cleanup_removes_the_run_and_is_idempotent(fixture_dir, tmp_path):
    out, expected = fixture_dir
    target = _migrated(out, tmp_path)
    result = cleanup(target, expected)
    assert result["clean"] and result["deleted"]["contacts"] == expected["exported_total"]
    assert result["deleted"]["list"] and result["deleted"]["campaign"]
    again = cleanup(target, expected)
    assert again["clean"] and again["deleted"] == {"contacts": 0, "campaign": False, "list": False}


def test_cleanup_leaves_foreign_members_and_their_list(fixture_dir, tmp_path):
    out, expected = fixture_dir
    target = _migrated(out, tmp_path)
    target.subscribers["someone@customer.test"] = TargetSubscriber(
        id=999, email="someone@customer.test", name="", status="enabled",
        subscription="confirmed", attribs={},
    )
    result = cleanup(target, expected)
    assert "someone@customer.test" in target.subscribers
    assert not result["deleted"]["list"]
    assert result["remaining"]["list"] and not result["clean"]


def test_cleanup_refuses_an_expected_file_outside_the_namespace(fixture_dir):
    _out, expected = fixture_dir
    tampered = {**expected, "emails": [*expected["emails"], "ceo@customer.test"]}
    with pytest.raises(CleanupRefused):
        cleanup(CleanableTarget(), tampered)
    with pytest.raises(CleanupRefused):
        cleanup(CleanableTarget(), {**expected, "list_name": "Customers (imported from mailchimp)"})


def test_brevo_deletes_hit_their_routes_and_treat_404_as_gone():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(404 if "lists" in request.url.path else 204)

    client = BrevoClient("k", transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    client.delete_contact(11)
    client.delete_campaign(12)
    client.delete_list(13)
    assert seen == [("DELETE", "/v3/contacts/11"), ("DELETE", "/v3/emailCampaigns/12"),
                    ("DELETE", "/v3/contacts/lists/13")]


def test_cli_cleanup_refusal_exits_2_before_any_client(fixture_dir, tmp_path, monkeypatch):
    _out, expected = fixture_dir
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**expected, "campaign_name": "Spring sale"}))

    def no_client(*_a, **_k):
        raise AssertionError("cleanup built a client for a refused record")

    monkeypatch.setattr(conformance_cli, "client_for_target", no_client)
    assert conformance_cli.main(["cleanup", "--expected", str(bad)]) == 2
