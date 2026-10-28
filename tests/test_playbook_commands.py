"""RC1-557: the playbook's three mechanical steps as commands -
`executor.run --verify-convergence`, a schedulable `executor.reconcile`
(exit codes, verdict line, --history), and `discovery.freeze_diff`.
FakeTarget for targets; every file written lives under tmp_path."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from discovery import freeze_diff as freeze_module
from discovery.freeze_diff import build_freeze_diff
from executor import reconcile as reconcile_module
from executor import run as run_module
from executor.plan import DesiredSubscriber
from ingest.mailchimp import bundle_fingerprint, load_bundle
from mapping.gate import GateError, approve, load_approved, load_spec, verify_source
from seeding.bundle_writer import make_campaign, write_audience_csvs, write_supplement
from seeding.generator import generate
from specs.model import MappingSpec
from tests.fake_target import FakeTarget

APPROVED = Path("specs/approved/mailchimp_to_listmonk.yaml")


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def run_cli(monkeypatch, module, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", argv)
    return module.main()


# --- 1. executor.run --verify-convergence ------------------------------------------------


class ForgetfulTarget(FakeTarget):
    """Acknowledges one write and never applies it - a run that 'succeeds'
    without converging, which only a second look can catch."""

    def create_subscriber(self, desired: DesiredSubscriber, list_id: int) -> None:
        if desired.email.lower() == self.forget:
            self.write_calls += 1
            return
        super().create_subscriber(desired, list_id)


def execute_args(journal: Path) -> list[str]:
    return ["executor.run", "--spec", str(APPROVED), "--execute",
            "--journal", str(journal), "--verify-convergence"]


def test_converged_run_is_proven_and_journaled(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_module, "client_for_target", lambda t, need_writes: FakeTarget())
    journal = tmp_path / "journal.jsonl"
    assert run_cli(monkeypatch, run_module, execute_args(journal)) == 0
    assert "converged: a second pass would write nothing" in capsys.readouterr().out
    (event,) = [e for e in lines(journal) if e["event"] == "convergence"]
    assert (event["converged"], event["pending"]) == (True, 0)


def test_a_run_that_did_not_converge_exits_1(tmp_path, monkeypatch, capsys):
    target = ForgetfulTarget()
    target.forget = "kbrewer@butler.info"
    monkeypatch.setattr(run_module, "client_for_target", lambda t, need_writes: target)
    journal = tmp_path / "journal.jsonl"
    assert run_cli(monkeypatch, run_module, execute_args(journal)) == 1
    assert "NOT CONVERGED: 1 write(s) still pending: create kbrewer@butler.info" in (
        capsys.readouterr().err
    )
    (event,) = [e for e in lines(journal) if e["event"] == "convergence"]
    assert event["converged"] is False and event["sample"] == ["create kbrewer@butler.info"]


def test_dry_run_verifies_the_target_as_it_stands(tmp_path, monkeypatch):
    target = FakeTarget()
    monkeypatch.setattr(run_module, "client_for_target", lambda t, need_writes: target)
    dry = ["executor.run", "--spec", str(APPROVED), "--journal",
           str(tmp_path / "j.jsonl"), "--verify-convergence"]
    assert run_cli(monkeypatch, run_module, dry) == 1  # nothing migrated yet
    assert run_cli(monkeypatch, run_module, execute_args(tmp_path / "j.jsonl")) == 0
    assert run_cli(monkeypatch, run_module, dry) == 0


def test_without_the_flag_nothing_changes(tmp_path, monkeypatch):
    target = ForgetfulTarget()
    target.forget = "kbrewer@butler.info"
    monkeypatch.setattr(run_module, "client_for_target", lambda t, need_writes: target)
    journal = tmp_path / "journal.jsonl"
    argv = ["executor.run", "--spec", str(APPROVED), "--execute", "--journal", str(journal)]
    assert run_cli(monkeypatch, run_module, argv) == 0
    assert [e for e in lines(journal) if e["event"] == "convergence"] == []


# --- 2. executor.reconcile, scheduled ------------------------------------------------------


@pytest.fixture()
def migrated(tmp_path, monkeypatch):
    target = FakeTarget()
    monkeypatch.setattr(run_module, "client_for_target", lambda t, need_writes: target)
    monkeypatch.setattr(reconcile_module, "client_for_target", lambda t, need_writes: target)
    journal = tmp_path / "journal.jsonl"
    assert run_cli(monkeypatch, run_module, execute_args(journal)) == 0
    return target, journal


def reconcile_args(journal: Path, history: Path) -> list[str]:
    return ["executor.reconcile", "--spec", str(APPROVED), "--journal", str(journal),
            "--history", str(history), "--out", str(history.with_suffix(".md"))]


def test_daily_pass_appends_history_and_ends_with_the_verdict(migrated, tmp_path, monkeypatch,
                                                                capsys):
    _, journal = migrated
    history = tmp_path / "parallel_run.jsonl"
    capsys.readouterr()
    for _ in range(2):  # two days of the window
        assert run_cli(monkeypatch, reconcile_module, reconcile_args(journal, history)) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == "reconcile PASS"
    records = lines(history)
    assert len(records) == 2 and all(r["passed"] and r["failing"] == [] for r in records)
    assert records[0]["suppression_pct"] == 100.0
    assert records[0]["spec_sha256"] == load_approved(APPROVED).approval.content_sha256


def test_a_failing_day_exits_1_and_names_the_checks(migrated, tmp_path, monkeypatch, capsys):
    target, journal = migrated
    suppressed = next(k for k, s in target.subscribers.items() if s.status == "blocklisted")
    target.subscribers[suppressed] = target.subscribers[suppressed].model_copy(
        update={"status": "enabled", "subscription": "confirmed"}
    )
    history = tmp_path / "parallel_run.jsonl"
    capsys.readouterr()
    assert run_cli(monkeypatch, reconcile_module, reconcile_args(journal, history)) == 1
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("reconcile FAIL: ") and "suppression" in last
    (record,) = lines(history)
    assert record["passed"] is False and "suppression" in record["failing"]


def test_a_run_that_cannot_reconcile_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        reconcile_module, "client_for_target", lambda t, need_writes: FakeTarget()
    )
    history = tmp_path / "parallel_run.jsonl"
    code = run_cli(monkeypatch, reconcile_module,
                   reconcile_args(tmp_path / "journal.jsonl", history))
    assert code == 2  # no list: a broken run, not a failed check
    out = capsys.readouterr()
    assert "reconcile ERROR" in out.out and "not found in the target" in out.err
    assert not history.exists()


# --- 3. discovery.freeze_diff --------------------------------------------------------------


def write_export(root: Path, subs, campaigns: list[dict]) -> tuple[Path, Path]:
    audience, supplement = root / "audience", root / "supplement"
    write_audience_csvs(subs, audience)
    write_supplement(supplement, campaigns=campaigns)
    return audience, supplement


def campaigns_for(subs) -> list[dict]:
    subscribed = sum(1 for s in subs if s.status == "subscribed")
    return [make_campaign("c1", "News", "News", "<p>Hi *|FNAME|*</p>", "L1", subscribed)]


@pytest.fixture()
def exports(tmp_path):
    """The reviewed export, its approved spec, and a fresh freeze-window
    export where members joined, left, and changed consent."""
    subs, _ = generate(300, seed=557)
    reviewed = write_export(tmp_path / "reviewed", subs, campaigns_for(subs))

    subscribed = [s for s in subs if s.status == "subscribed"]
    unsubscribed = [s for s in subs if s.status == "unsubscribed"]
    went_quiet = {s.email for s in subscribed[:5]}
    came_back = {s.email for s in unsubscribed[:2]}
    left = {s.email for s in subscribed[5:8]}
    fresh_subs = [
        s.model_copy(update={"status": "unsubscribed"}) if s.email in went_quiet
        else s.model_copy(update={"status": "subscribed"}) if s.email in came_back
        else s
        for s in subs if s.email not in left
    ]
    known = {s.email for s in subs}
    joined = [s for s in generate(40, seed=5570)[0]
              if s.status == "subscribed" and s.email not in known][:4]
    fresh_subs += joined
    fresh = write_export(tmp_path / "fresh", fresh_subs, campaigns_for(subs))

    data = load_approved(APPROVED).model_dump(mode="json")
    data.update(approval=None, status="draft",
                source_fingerprint=bundle_fingerprint(*reviewed),
                source_timezone="America/New_York")
    spec_path = tmp_path / "approved.yaml"
    spec_path.write_text(
        approve(MappingSpec.model_validate(data), approved_by="spec owner").to_yaml()
    )
    return {
        "reviewed": reviewed, "fresh": fresh, "spec": spec_path, "went_quiet": went_quiet,
        "came_back": came_back, "left": left, "joined": {s.email for s in joined},
    }


def freeze_args(ex, *, fresh=None, draft: Path) -> list[str]:
    audience, supplement = fresh or ex["fresh"]
    return ["discovery.freeze_diff", "--spec", str(ex["spec"]),
            "--reviewed-audience-dir", str(ex["reviewed"][0]),
            "--reviewed-supplement-dir", str(ex["reviewed"][1]),
            "--audience-dir", str(audience), "--supplement-dir", str(supplement),
            "--draft-out", str(draft)]


def test_the_delta_is_counted_exactly(exports):
    spec = load_approved(exports["spec"])
    diff = build_freeze_diff(load_bundle(*exports["reviewed"]), load_bundle(*exports["fresh"]),
                             spec)
    assert set(diff.added) == {e.lower() for e in exports["joined"]}
    assert set(diff.removed) == {e.lower() for e in exports["left"]}
    assert set(diff.new_suppressions) == {e.lower() for e in exports["went_quiet"]}
    assert set(diff.resubscribes) == {e.lower() for e in exports["came_back"]}
    assert diff.structural == []
    assert diff.fresh_counts["subscribed"] - diff.reviewed_counts["subscribed"] == (
        len(exports["joined"]) - len(exports["left"]) - len(exports["went_quiet"])
        + len(exports["came_back"])
    )


def test_data_only_delta_writes_a_draft_only_a_human_can_approve(exports, tmp_path,
                                                                   monkeypatch, capsys):
    draft_path = tmp_path / "draft.yaml"
    assert run_cli(monkeypatch, freeze_module, freeze_args(exports, draft=draft_path)) == 0
    assert "**DATA ONLY**" in capsys.readouterr().out

    draft = load_spec(draft_path)
    approved = load_approved(exports["spec"])
    assert draft.approval is None and draft.status.value == "draft"
    assert draft.source_fingerprint == bundle_fingerprint(*exports["fresh"])
    rebound = {"approval", "status", "source_fingerprint", "source_timezone"}
    assert draft.model_dump(exclude=rebound) == approved.model_dump(exclude=rebound)
    # The old approval never covers the fresh export; the re-signed draft does.
    with pytest.raises(GateError, match="does not match the approved spec"):
        verify_source(approved, *exports["fresh"], "America/New_York")
    resigned = approve(draft, approved_by="spec owner")
    verify_source(resigned, *exports["fresh"], "America/New_York")


def test_a_structural_change_writes_no_draft(exports, tmp_path, monkeypatch, capsys):
    subs, _ = generate(300, seed=557)
    fresh = write_export(
        tmp_path / "structural", subs,
        campaigns_for(subs) + [make_campaign("c2", "Flash sale", "Sale", "<p>Sale</p>",
                                             "L1", 10)],
    )
    draft_path = tmp_path / "draft.yaml"
    code = run_cli(monkeypatch, freeze_module,
                   freeze_args(exports, fresh=fresh, draft=draft_path))
    assert code == 1
    out = capsys.readouterr()
    assert "**STRUCTURAL - re-propose**: campaigns changed" in out.out
    assert "Campaigns: added Flash sale" in out.out
    assert not draft_path.exists()


def test_the_comparison_base_must_be_the_reviewed_export(exports, tmp_path, monkeypatch,
                                                          capsys):
    impostor = tmp_path / "impostor"
    shutil.copytree(exports["fresh"][0], impostor / "audience")
    shutil.copytree(exports["fresh"][1], impostor / "supplement")
    argv = freeze_args(exports, draft=tmp_path / "draft.yaml")
    argv[argv.index("--reviewed-audience-dir") + 1] = str(impostor / "audience")
    argv[argv.index("--reviewed-supplement-dir") + 1] = str(impostor / "supplement")
    assert run_cli(monkeypatch, freeze_module, argv) == 2
    assert "does not match the approved spec" in capsys.readouterr().err
