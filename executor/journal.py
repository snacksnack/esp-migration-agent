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
error, response text included) or `run_complete` (the summary).
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

from executor.engine import ExecutionSummary, PlanDiff
from specs.model import MappingSpec


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
    }


class ExecutionJournal:
    def __init__(self, path: Path):
        self.path = path
        self.run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")

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
        self._event(
            "run_start",
            target=target,
            spec=spec,
            source=source,
            destination=destination,
            list_name=list_name,
        )

    def plan(self, diff: PlanDiff) -> None:
        self._event("plan", counts=diff.counts())

    def write(self, phase: str, action: str, key: str, recovered: bool = False) -> None:
        fields: dict = {"phase": phase, "action": action, "key": key}
        if recovered:  # only worth a byte when true
            fields["recovered"] = True
        self._event("write", **fields)

    def conflict(self, phase: str, key: str) -> None:
        self._event("conflict", phase=phase, key=key)

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
