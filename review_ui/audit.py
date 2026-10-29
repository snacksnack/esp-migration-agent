"""Audit log of review-gate decisions (RC1-554, ADR-0021).

Append-only JSONL, one record per decision: sign-ins and sign-outs, access
the IdP took back, each mapping resolution, each approval and each refusal.
Every record names its actor as the session established it (verified SSO
identity, unverified password session, or anonymous) - never a typed name
alone.

Records are hash-chained: each carries the sha256 of the previous record and
its own sha256 over its canonical JSON, so an edited or deleted line breaks
verify_chain() from that point on. Tamper-evident, not tamper-proof - whoever
can rewrite the file can rewrite the chain; shipping each line to stdout
(Fly's log stream) is the out-of-band copy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

from specs.model import ApproverIdentity

GENESIS = "0" * 64

logger = logging.getLogger("esp.review_audit")


def _digest(record: dict) -> str:
    body = {k: v for k, v in record.items() if k != "sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def actor_of(identity: ApproverIdentity | None) -> dict:
    if identity is None:
        return {"method": "anonymous", "verified": False}
    return identity.model_dump(mode="json", exclude_none=True)


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._seq, self._prev = 0, GENESIS
        if path.exists():
            lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
            if lines:
                last = json.loads(lines[-1])
                self._seq, self._prev = last["seq"], last["sha256"]

    def record(self, event: str, actor: ApproverIdentity | None, **detail) -> dict:
        with self._lock:
            entry = {
                "seq": self._seq + 1,
                "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "event": event,
                "actor": actor_of(actor),
                "detail": detail,
                "prev_sha256": self._prev,
            }
            entry["sha256"] = _digest(entry)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
            self._seq, self._prev = entry["seq"], entry["sha256"]
        logger.info("audit %s", json.dumps(entry, sort_keys=True))
        return entry

    def read(self, limit: int = 100) -> list[dict]:
        """Newest first."""
        if not self.path.exists():
            return []
        lines = [ln for ln in self.path.read_text().splitlines() if ln.strip()]
        return [json.loads(ln) for ln in reversed(lines[-limit:])]


def verify_chain(path: Path) -> list[str]:
    """Problems found walking the chain, in file order; empty means intact."""
    problems: list[str] = []
    prev, expected_seq = GENESIS, 1
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"line {lineno}: not JSON")
            break
        if record.get("prev_sha256") != prev:
            problems.append(f"line {lineno}: chain broken (prev_sha256 does not match)")
        if record.get("sha256") != _digest(record):
            problems.append(f"line {lineno}: record altered (sha256 does not match)")
        if record.get("seq") != expected_seq:
            problems.append(f"line {lineno}: seq {record.get('seq')}, expected {expected_seq}")
        prev, expected_seq = record.get("sha256"), (record.get("seq") or 0) + 1
    return problems
