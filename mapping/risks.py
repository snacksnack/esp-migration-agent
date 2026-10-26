"""Risk-register tie-in (RC1-545): flagged mappings become register entries.

Discovery risks (RC1-544) and mapping open questions read as one
severity-ranked log - the engagement carries a single risk register end to
end. Deterministic: a risk here is a restatement of a flagged mapping, never
new model judgment.
"""

from __future__ import annotations

from discovery.risks import Risk, Severity
from mapping.gate import mapping_label
from specs.model import MappingSpec, MappingStatus


def spec_risks(spec: MappingSpec) -> list[Risk]:
    return [
        Risk(
            code="mapping_open_question",
            severity=Severity.MEDIUM,
            title=f"Open mapping question: {mapping_label(m)}",
            detail=m.open_question or "",
        )
        for m in spec.reviewed_mappings()
        if m.status is MappingStatus.FLAGGED
    ]
