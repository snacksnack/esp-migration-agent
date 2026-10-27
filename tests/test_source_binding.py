"""RC1-566 (ADR-0014): the approval binds the inputs. The source bundle is
fingerprinted at proposal time, the approval's content hash covers it, and
the executor/reconciliation refuse a different bundle or a mismatched
--target. Pre-RC1-566 specs (no fingerprint) keep their hash and still run."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from executor import run as run_module
from ingest.mailchimp import bundle_fingerprint
from mapping.gate import GateError, load_approved, verify_source
from tests.fake_target import FakeTarget

AUDIENCE = Path("fixtures/mailchimp_export/audience")
SUPPLEMENT = Path("fixtures/api_supplement")
APPROVED = Path("specs/approved/mailchimp_to_listmonk.yaml")


def test_fingerprint_is_stable_and_byte_sensitive(tmp_path):
    assert bundle_fingerprint(AUDIENCE, SUPPLEMENT) == bundle_fingerprint(
        AUDIENCE, SUPPLEMENT
    )
    tampered = tmp_path / "audience"
    shutil.copytree(AUDIENCE, tampered)
    victim = sorted(tampered.iterdir())[0]
    victim.write_bytes(victim.read_bytes() + b" ")
    assert bundle_fingerprint(tampered, SUPPLEMENT) != bundle_fingerprint(
        AUDIENCE, SUPPLEMENT
    )


def test_pre_rc1_566_spec_still_loads_and_passes_with_a_note(capsys):
    # The committed approved spec has no fingerprint: its content hash must
    # be unchanged by the new field, and verify_source lets it through loudly.
    spec = load_approved(APPROVED)
    assert spec.source_fingerprint is None
    verify_source(spec, AUDIENCE, SUPPLEMENT)
    assert "not verified" in capsys.readouterr().err
    assert "source_fingerprint" not in spec.to_yaml()


def test_fingerprinted_spec_round_trips_and_rebinds_the_hash():
    spec = load_approved(APPROVED)
    stamped = spec.model_copy(
        update={"source_fingerprint": bundle_fingerprint(AUDIENCE, SUPPLEMENT)}
    )
    assert stamped.content_sha256() != spec.content_sha256()
    reparsed = type(spec).from_yaml(stamped.to_yaml())
    assert reparsed.source_fingerprint == stamped.source_fingerprint


def test_verify_source_refuses_a_different_bundle():
    spec = load_approved(APPROVED).model_copy(
        update={"source_fingerprint": "0" * 64}
    )
    with pytest.raises(GateError, match="does not match the approved spec"):
        verify_source(spec, AUDIENCE, SUPPLEMENT)


def test_verify_source_accepts_the_reviewed_bundle():
    spec = load_approved(APPROVED).model_copy(
        update={"source_fingerprint": bundle_fingerprint(AUDIENCE, SUPPLEMENT)}
    )
    verify_source(spec, AUDIENCE, SUPPLEMENT)  # must not raise


def test_cli_refuses_a_target_mismatch(monkeypatch, capsys):
    fake = FakeTarget()
    monkeypatch.setattr(
        run_module, "client_for_target", lambda target, need_writes: fake
    )
    monkeypatch.setattr(
        sys, "argv", ["executor.run", "--spec", str(APPROVED), "--target", "brevo"]
    )
    assert run_module.main() == 1
    assert "does not match the approved spec's target" in capsys.readouterr().err
    assert fake.lists == {}
