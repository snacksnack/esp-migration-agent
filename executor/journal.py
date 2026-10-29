"""Durable execution journal (RC1-571): the run's record, not its printout.

Append-only JSONL, one event per line, flushed as written - a run killed
mid-write leaves a journal that ends exactly where the run did, and the
resuming run appends to the same file, so convergence is provable from
records instead of asserted from memory (ADR-0016).

Events: `run_start` (spec provenance, source inputs, DESTINATION identity -
recorded here rather than hard-bound at approval, because the destination
often does not exist at review time), `plan` (the diff's counts), one `write`
per completed write (phase, action, key; `recovered: true` when the adapter's
read-back stood in for a lost response, RC1-570), `conflict` per address kept
at the destination's stricter consent (ADR-0013), and `run_failed` (the last
error, response text included) or `run_complete` (the summary), and with
--verify-convergence a `convergence` event: the read-only re-diff (RC1-557).

`baseline` (RC1-574, ADR-0019): the destination as the migration found it -
every member of the list with its consent, plus account-wide records for
planned addresses not on it - captured after the first read and before the
first write, ONLY while no write to that destination list has ever been
journaled. Later runs would see the migration's own writes, so the first
baseline is the only one; reconcile reads it back with `find_baseline`.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from executor.engine import ExecutionSummary, PlanDiff
from executor.target import TargetSubscriber
from specs.model import MappingSpec


class Baseline(BaseModel):
    """Pre-migration state of one destination list (RC1-574)."""

    run_id: str
    list_id: int
    members: dict[str, tuple[str, str]]  # email -> (status, subscription) on the list
    elsewhere: dict[str, str]  # planned email off the list -> global status


class BaselineLookup(BaseModel):
    baseline: Baseline | None
    written_before: bool = False  # writes predate any baseline: none can be taken now
    reason: str = ""  # why there is none (empty when there is one)


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def find_baseline(path: Path, destination: dict, list_name: str) -> BaselineLookup:
    """The first baseline journaled for this destination list - and only if no
    write to it was journaled before that baseline (a baseline taken after the
    migration wrote would count the migration's own records as pre-existing)."""
    runs: set[str] = set()
    for event in read_events(path):
        if event["event"] == "run_start":
            if event.get("destination") == destination and event.get("list_name") == list_name:
                runs.add(event["run_id"])
        elif event.get("run_id") not in runs:
            continue
        elif event["event"] == "baseline":
            return BaselineLookup(
                baseline=Baseline.model_validate(
                    {k: event[k] for k in ("run_id", "list_id", "members", "elsewhere")}
                )
            )
        elif event["event"] == "write":
            return BaselineLookup(
                baseline=None,
                written_before=True,
                reason="the journal records writes to this list before any baseline "
                "(migrated before RC1-574); destination assumed empty",
            )
    if not runs:
        return BaselineLookup(
            baseline=None,
            reason=f"no journaled run for list {list_name!r} on this destination; "
            "destination assumed empty",
        )
    return BaselineLookup(baseline=None, reason="no baseline journaled; destination assumed empty")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def spec_provenance(path: Path, spec: MappingSpec) -> dict:
    """Everything the approval binds, so the journal alone answers what ran:
    which reviewed content (hash), over which export (fingerprint), parsed
    how (timezone), signed by whom."""
    approval = spec.approval
    if approval is None:  # load_approved guarantees otherwise; belt and braces
        raise ValueError("journal requires an approved spec")
    return {
        "path": str(path),
        "content_sha256": approval.content_sha256,
        "source_fingerprint": spec.source_fingerprint,
        "source_timezone": spec.source_timezone,
        "approved_by": approval.approved_by,
        "approved_at": approval.approved_at.isoformat(),
        # RC1-554: how the approver authenticated; absent on older approvals.
        **(
            {"approver_identity": approval.identity.model_dump(mode="json")}
            if approval.identity
            else {}
        ),
    }


class ExecutionJournal:
    def __init__(self, path: Path):
        self.path = path
        self.run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self._destination: dict | None = None
        self._list_name: str | None = None

    def _event(self, event: str, **fields) -> None:
        record = {"event": event, "run_id": self.run_id, "at": _now(), **fields}
        self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._fh.flush()  # a crash after this line still leaves the event

    def run_start(
        self,
        *,
        target: str,
        spec: dict,
        source: dict,
        destination: dict,
        list_name: str,
    ) -> None:
        self._destination, self._list_name = destination, list_name
        self._event(
            "run_start",
            target=target,
            spec=spec,
            source=source,
            destination=destination,
            list_name=list_name,
        )

    def baseline(
        self,
        list_id: int,
        current: dict[str, TargetSubscriber],
        elsewhere: dict[str, TargetSubscriber],
    ) -> bool:
        """Journal the destination as found, if this is still the first look
        at it. Needs run_start (the destination identity); returns whether a
        baseline was written."""
        if self._destination is None or self._list_name is None:
            return False
        self._fh.flush()
        prior = find_baseline(self.path, self._destination, self._list_name)
        if prior.baseline is not None or prior.written_before:
            return False
        self._event(
            "baseline",
            list_id=list_id,
            members={k: [s.status, s.subscription] for k, s in sorted(current.items())},
            elsewhere={k: s.status for k, s in sorted(elsewhere.items())},
        )
        return True

    def plan(self, diff: PlanDiff) -> None:
        self._event("plan", counts=diff.counts())

    def write(self, phase: str, action: str, key: str, recovered: bool = False) -> None:
        fields: dict = {"phase": phase, "action": action, "key": key}
        if recovered:  # only worth a byte when true
            fields["recovered"] = True
        self._event("write", **fields)

    def conflict(self, phase: str, key: str) -> None:
        self._event("conflict", phase=phase, key=key)

    def convergence(self, diff: PlanDiff) -> None:
        """RC1-557: the read-only re-diff after the run - proof, in the
        record, that a second pass would write nothing (or what it would)."""
        pending = diff.writes
        self._event(
            "convergence",
            converged=not pending,
            pending=len(pending),
            sample=[f"{e.action} {e.key}" for e in pending[:10]],
        )

    def run_failed(self, error: str) -> None:
        self._event("run_failed", error=error)

    def run_complete(self, summary: ExecutionSummary) -> None:
        self._event(
            "run_complete",
            list_id=summary.list_id,
            written=summary.written,
            skipped_unchanged=summary.skipped_unchanged,
            conflicts=len(summary.conflicts),
        )

    def close(self) -> None:
        self._fh.close()
