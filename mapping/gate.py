"""The human review gate (RC1-545): approval mechanics around the YAML spec.

Per ADR-0003 the review surface IS the YAML document - the reviewer edits it
directly (resolving each mapping to approved or edited, answering open
questions). This module holds what discipline cannot: approve() refuses a spec
with unresolved mappings, pins a sha256 of exactly what was reviewed, and
load_approved() is the ONLY door downstream stages may read a spec through.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from specs.model import MappingSpec, MappingStatus, SpecStatus, hash_content

UNRESOLVED = (MappingStatus.PROPOSED, MappingStatus.FLAGGED)


class GateError(RuntimeError):
    pass


def mapping_label(mapping) -> str:
    """Human-readable identity of a mapping, whatever its kind."""
    for attr in ("source_field", "source_segment", "source_status"):
        value = getattr(mapping, attr, None)
        if value:
            return f"{attr.removeprefix('source_')} {value!r}"
    return "unknown mapping"


def load_spec(path: Path) -> MappingSpec:
    """Parse and validate a spec file, loudly."""
    try:
        return MappingSpec.from_yaml(path.read_text())
    except FileNotFoundError as exc:
        raise GateError(f"no spec at {path}") from exc
    except (ValidationError, yaml.YAMLError) as exc:
        raise GateError(f"spec at {path} does not validate: {exc}") from exc


def approve(spec: MappingSpec, approved_by: str, now: datetime | None = None) -> MappingSpec:
    """Stamp an approval onto a fully reviewed spec.

    The content hash is computed over the spec exactly as it will persist
    (status already approved, approval block excluded), so load_approved can
    recompute and compare byte-for-byte semantics."""
    unresolved = [m for m in spec.reviewed_mappings() if m.status in UNRESOLVED]
    if unresolved:
        labels = ", ".join(mapping_label(m) for m in unresolved)
        raise GateError(
            f"cannot approve: {len(unresolved)} unresolved mapping(s) ({labels}). "
            "Resolve each to approved/edited in the YAML, answering any open questions."
        )
    content = spec.model_dump(mode="json", exclude={"approval"})
    content["status"] = SpecStatus.APPROVED.value
    approval = {
        "approved_by": approved_by,
        "approved_at": (now or datetime.now(UTC)).isoformat(),
        "content_sha256": hash_content(content),
    }
    return MappingSpec.model_validate({**content, "approval": approval})


def load_approved(path: Path) -> MappingSpec:
    """The only entry point downstream stages (RC1-546 executor onward) may
    use. Refuses anything except an approved, untampered spec."""
    spec = load_spec(path)
    if spec.status is not SpecStatus.APPROVED or spec.approval is None:
        raise GateError(
            f"spec at {path} is {spec.status}, not approved; "
            "downstream stages only read approved specs"
        )
    if spec.content_sha256() != spec.approval.content_sha256:
        raise GateError(
            f"spec at {path} was modified after approval (content hash mismatch); "
            "re-review and re-approve it"
        )
    return spec
