"""Telemetry is decoration: everything here runs with no ddtrace and no key."""

from __future__ import annotations

import httpx
import pytest

from telemetry import count, enable_llmobs, stage, workflow
from telemetry.llmobs import package_version
from telemetry.metrics import METRIC_PREFIX, series_payload


def test_count_declines_without_a_key(monkeypatch):
    """No DD_API_KEY means no POST and no error - the run is unaffected."""
    monkeypatch.delenv("DD_API_KEY", raising=False)
    monkeypatch.setattr(httpx, "post", _refuse_network)
    assert count("pipeline_run", tags={"stage": "discovery"}) is False


def test_workflow_and_enable_are_noops_without_ddtrace(monkeypatch):
    """CI has no `obs` group installed, so this exercises the decline path."""
    monkeypatch.delenv("DD_API_KEY", raising=False)
    assert enable_llmobs() is False
    with workflow("mapping.propose"):
        pass  # must not raise


def test_series_payload_shape():
    """type is the v2 integer enum (1 = count) and the base tags carry
    service, env, and version - the tag the intake indexes by (RC1-407)."""
    payload = series_payload("gate_approval", 1, ["outcome:approved"], at=1700000000)
    (series,) = payload["series"]
    assert series["metric"] == METRIC_PREFIX + "gate_approval"
    assert series["type"] == 1
    assert series["points"] == [{"timestamp": 1700000000, "value": 1}]
    assert "outcome:approved" in series["tags"]
    assert any(t.startswith("version:") for t in series["tags"])
    assert "service:esp-migration-agent" in series["tags"]


def test_rejected_post_is_loud_but_not_fatal(monkeypatch, capsys):
    """A 400 is printed, never swallowed and never raised: a swallowed 400
    looks exactly like no traffic on every dashboard downstream."""
    monkeypatch.setenv("DD_API_KEY", "test-key-not-a-credential")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(400, text="bad payload"))
    assert count("pipeline_run") is False
    assert "HTTP 400" in capsys.readouterr().err


def test_stage_counts_failure_and_reraises(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "telemetry.metrics.count", lambda name, value=1, tags=None: sent.append((name, tags))
    )
    with pytest.raises(ValueError), stage("execute", mode="dry_run"):
        raise ValueError("boom")
    assert sent == [("pipeline_run", {"stage": "execute", "outcome": "failure", "mode": "dry_run"})]


def test_stage_counts_success(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "telemetry.metrics.count", lambda name, value=1, tags=None: sent.append((name, tags))
    )
    with stage("reconcile", target="listmonk"):
        pass
    assert sent == [
        ("pipeline_run", {"stage": "reconcile", "outcome": "success", "target": "listmonk"})
    ]


def test_package_version_reads_pyproject():
    assert package_version()[0].isdigit()


def _refuse_network(*args, **kwargs):
    raise AssertionError("count() must not touch the network without a key")
