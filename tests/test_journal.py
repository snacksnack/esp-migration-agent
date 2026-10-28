"""RC1-571: the execution journal - run provenance, destination identity,
per-write records that survive an interruption, recovered/conflict marking,
and the CLI wiring (journal on --execute only). FakeTarget and MockTransport
throughout; the only files touched live under tmp_path."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from executor import run as run_module
from executor.brevo import BrevoClient
from executor.engine import execute
from executor.journal import ExecutionJournal
from executor.listmonk import ListmonkClient
from executor.plan import DesiredSubscriber, MigrationPlan, PlannedCampaign
from executor.target import OfflineTarget, TargetError
from mapping.gate import load_approved
from tests.fake_target import FakeTarget

APPROVED = Path("specs/approved/mailchimp_to_listmonk.yaml")


def subscriber(i: int, *, suppressed: bool) -> DesiredSubscriber:
    return DesiredSubscriber(
        email=f"{'sup' if suppressed else 'sub'}{i}@example.com",
        name=f"Sub {i}",
        status="blocklisted" if suppressed else "enabled",
        subscription="unsubscribed" if suppressed else "confirmed",
        subscribable=not suppressed,
        suppressed=suppressed,
        attribs={},
    )


def small_plan() -> MigrationPlan:
    return MigrationPlan(
        source="mailchimp",
        target="listmonk",
        list_name="Migrated",
        suppressions=[subscriber(i, suppressed=True) for i in range(2)],
        subscribers=[subscriber(i, suppressed=False) for i in range(3)],
        campaigns=[PlannedCampaign(name="Welcome", subject="Hi", body="<p>Hi</p>")],
        segments=[],
    )


def events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


# --- the engine journals every completed write, in phase order ------------------


def test_every_completed_write_is_journaled_in_phase_order(tmp_path):
    journal = ExecutionJournal(tmp_path / "journal.jsonl")
    execute(small_plan(), FakeTarget(), journal)
    writes = [e for e in events(journal.path) if e["event"] == "write"]
    assert [w["key"] for w in writes] == [
        "sup0@example.com", "sup1@example.com",  # phase 1 first, always
        "sub0@example.com", "sub1@example.com", "sub2@example.com",
        "Welcome",
    ]
    assert {w["phase"] for w in writes} == {
        "1-suppressions", "2-subscribers", "3-campaigns"
    }
    assert all(w["action"] == "create" and "recovered" not in w for w in writes)
    assert all(w["run_id"] == journal.run_id for w in writes)


def test_converged_run_journals_no_writes(tmp_path):
    target = FakeTarget()
    execute(small_plan(), target)  # first run unjournaled on purpose
    journal = ExecutionJournal(tmp_path / "journal.jsonl")
    execute(small_plan(), target, journal)
    assert [e for e in events(journal.path) if e["event"] == "write"] == []


# --- interruption: the record ends where the run did, the resume completes it ----


class DyingTarget(FakeTarget):
    def __init__(self, die_after: int):
        super().__init__()
        self.die_after: int | None = die_after

    def _store(self, desired: DesiredSubscriber, sub_id: int) -> None:
        if self.die_after is not None and self.write_calls >= self.die_after:
            raise TargetError("create subscriber: HTTP 503 after 4 attempts")
        super()._store(desired, sub_id)


def test_interrupted_and_resumed_runs_share_one_provable_record(tmp_path):
    path = tmp_path / "journal.jsonl"
    target = DyingTarget(die_after=3)
    plan = small_plan()

    first = ExecutionJournal(path)
    with pytest.raises(TargetError) as exc:
        execute(plan, target, first)
    first.run_failed(str(exc.value))  # as executor.run does

    target.die_after = None
    second = ExecutionJournal(path)  # same file, new run
    second.run_complete(execute(plan, target, second))

    all_events = events(path)
    first_writes = [
        e["key"] for e in all_events
        if e["event"] == "write" and e["run_id"] == first.run_id
    ]
    second_writes = [
        e["key"] for e in all_events
        if e["event"] == "write" and e["run_id"] == second.run_id
    ]
    # The failed run's record ends with its last landed write, then the error.
    assert len(first_writes) == 3
    failed = [e for e in all_events if e["event"] == "run_failed"]
    assert len(failed) == 1 and "HTTP 503" in failed[0]["error"]
    # Convergence from records: disjoint writes, together exactly the plan.
    assert set(first_writes).isdisjoint(second_writes)
    assert set(first_writes) | set(second_writes) == {
        d.email for d in plan.all_subscribers
    } | {"Welcome"}
    complete = [e for e in all_events if e["event"] == "run_complete"]
    assert len(complete) == 1 and complete[0]["run_id"] == second.run_id


# --- RC1-570 ambiguous outcomes journal as such ----------------------------------


class RecoveringTarget(FakeTarget):
    """Pretends one write's response was lost and recovered by read-back,
    the way an adapter reports it: recovered_calls ticks up."""

    recovered_calls = 0

    def create_subscriber(self, desired: DesiredSubscriber, list_id: int) -> None:
        super().create_subscriber(desired, list_id)
        if desired.email == "sub1@example.com":
            self.recovered_calls += 1


def test_recovered_write_is_marked(tmp_path):
    journal = ExecutionJournal(tmp_path / "journal.jsonl")
    execute(small_plan(), RecoveringTarget(), journal)
    writes = {e["key"]: e for e in events(journal.path) if e["event"] == "write"}
    assert writes["sub1@example.com"]["recovered"] is True
    assert "recovered" not in writes["sub2@example.com"]


# --- conflicts kept are part of the record ---------------------------------------


def test_kept_conflict_is_journaled(tmp_path):
    target = FakeTarget()
    list_id = target.ensure_list("Migrated")
    protected = subscriber(0, suppressed=False)
    target.create_subscriber(
        DesiredSubscriber(
            email=protected.email,
            name=protected.name,
            status="blocklisted",
            subscription="unsubscribed",
            subscribable=False,
            suppressed=True,
            attribs={},
        ),
        list_id,
    )
    journal = ExecutionJournal(tmp_path / "journal.jsonl")
    summary = execute(small_plan(), target, journal)
    conflict_events = [e for e in events(journal.path) if e["event"] == "conflict"]
    assert [e["key"] for e in conflict_events] == [protected.email]
    assert summary.conflicts == [protected.email]
    assert isinstance(target.subscribers[protected.email].id, int)  # kept, not rewritten


# --- CLI wiring: --execute journals, dry run does not ----------------------------


def test_execute_journals_provenance_and_destination(tmp_path, monkeypatch):
    fake = FakeTarget()
    monkeypatch.setattr(
        run_module, "client_for_target", lambda target, need_writes: fake
    )
    journal_path = tmp_path / "journal.jsonl"
    monkeypatch.setattr(
        sys,
        "argv",
        ["executor.run", "--spec", str(APPROVED), "--execute",
         "--journal", str(journal_path)],
    )
    assert run_module.main() == 0
    recorded = events(journal_path)
    assert [e["event"] for e in recorded[:2]] == ["run_start", "plan"]
    start = recorded[0]
    spec = load_approved(APPROVED)
    assert start["spec"]["content_sha256"] == spec.approval.content_sha256
    assert start["spec"]["source_fingerprint"] == spec.source_fingerprint
    assert start["spec"]["source_timezone"] == spec.source_timezone
    assert start["spec"]["approved_by"] == spec.approval.approved_by
    assert start["destination"] == {"platform": "fake", "host": "in-memory"}
    assert start["target"] == "listmonk"
    assert recorded[1]["counts"]["1-suppressions"]["create"] == 411
    assert recorded[-1]["event"] == "run_complete"
    assert recorded[-1]["written"] == {"create": 4258}
    writes = [e for e in recorded if e["event"] == "write"]
    assert len(writes) == 4258


def test_dry_run_writes_no_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(
        run_module, "client_for_target", lambda target, need_writes: FakeTarget()
    )
    journal_path = tmp_path / "journal.jsonl"
    monkeypatch.setattr(
        sys,
        "argv",
        ["executor.run", "--spec", str(APPROVED), "--journal", str(journal_path)],
    )
    assert run_module.main() == 0
    assert not journal_path.exists()


# --- destination identity per adapter --------------------------------------------


def test_listmonk_destination_identity():
    client = ListmonkClient("http://listmonk.test:9000", "api-user", "token")
    assert client.describe_destination() == {
        "platform": "listmonk",
        "host": "http://listmonk.test:9000/api",
        "api_user": "api-user",
    }


def test_brevo_destination_identity_names_the_account():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/account")
        return httpx.Response(
            200, json={"email": "owner@example.com", "companyName": "ESP Demo"}
        )

    client = BrevoClient("test-key", transport=httpx.MockTransport(handler))
    assert client.describe_destination() == {
        "platform": "brevo",
        "host": "api.brevo.com",
        "account_email": "owner@example.com",
        "organization": "ESP Demo",
    }


def test_fake_and_offline_targets_identify_themselves():
    assert OfflineTarget().describe_destination() == {"platform": "offline"}
    assert FakeTarget().describe_destination()["platform"] == "fake"
