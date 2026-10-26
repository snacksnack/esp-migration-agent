"""Scope-and-estimate proposal, generated from the deterministic inventory
(RC1-544). The artifact a professional-services team sells from: workstreams,
per-workstream effort driven by inventory counts, explicit assumptions, and an
explicit out-of-scope list. The LLM may draft surrounding prose; every number
here comes from Python.

Effort model: transparent per-unit constants, stated inline. They are planning
anchors to adjust in review, not promises - which is exactly how a real
services estimate works.
"""

from __future__ import annotations

from pydantic import BaseModel

from discovery.inventory import Inventory
from discovery.risks import Risk


class Workstream(BaseModel):
    name: str
    driver: str  # which inventory count drives the estimate
    units: int
    est_hours: float


class Proposal(BaseModel):
    workstreams: list[Workstream]
    total_hours: float
    assumptions: list[str]
    out_of_scope: list[str]


def _round_half(x: float) -> float:
    return round(x * 2) / 2


def build_proposal(inventory: Inventory, risks: list[Risk]) -> Proposal:
    data_quality_rows = sum(r.count or 0 for r in risks if r.severity == "medium" and r.count)
    flagged_segments = sum(1 for r in risks if r.code == "unsupported_segment")
    translatable_segments = len(inventory.segments) - flagged_segments

    workstreams = [
        Workstream(
            name="Suppression migration (runs first, gating)",
            driver="suppression_count",
            units=inventory.suppression_count,
            # Fixed setup + verification sampling; volume barely moves it.
            est_hours=_round_half(4 + inventory.suppression_count / 2000),
        ),
        Workstream(
            name="Subscriber and merge-field migration",
            driver="members_total",
            units=inventory.members_total,
            # Batch load + per-10k reconciliation pass.
            est_hours=_round_half(8 + inventory.members_total / 10_000 * 2),
        ),
        Workstream(
            name="Data cleanup (malformed merge-field values)",
            driver="data_quality_risk_rows",
            units=data_quality_rows,
            # Rule-writing dominates; volume adds verification time.
            est_hours=_round_half(6 + data_quality_rows / 500),
        ),
        Workstream(
            name="Segment translation",
            driver="segments",
            units=len(inventory.segments),
            # 1h per translatable segment; 4h per flagged one (needs a
            # human decision plus a rebuild or workaround).
            est_hours=_round_half(translatable_segments * 1 + flagged_segments * 4),
        ),
        Workstream(
            name="Template conversion (merge-tag translation)",
            driver="templates",
            units=len(inventory.templates),
            est_hours=_round_half(len(inventory.templates) * 3),
        ),
        Workstream(
            name="Validation and cutover",
            driver="fixed",
            units=1,
            est_hours=8.0,
        ),
    ]
    return Proposal(
        workstreams=workstreams,
        total_hours=_round_half(sum(w.est_hours for w in workstreams)),
        assumptions=[
            "Counts come from the export bundle as of the discovery date; deltas "
            "accumulated between export and cutover migrate in a final sync pass.",
            "Pending (unconfirmed) members are absent from the export and are "
            "reconciled against API counts before cutover (see risk register).",
            "No sent-campaign history migrates; the target starts with a clean "
            "sending reputation.",
            "Estimates are planning anchors per the stated per-unit rates, to be "
            "reviewed with the customer.",
        ],
        out_of_scope=[
            "Automations and journeys (source account has none in the export; "
            "if present they are a separate engagement - RC1-545 stretch).",
            "Historical campaign archive migration.",
            "Deliverability warm-up execution on the target (a ramp "
            "recommendation is included in the discovery report).",
        ],
    )
