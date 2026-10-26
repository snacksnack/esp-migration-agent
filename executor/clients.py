"""Target-client factory (RC1-547): one --target flag picks the adapter; the
plan, engine, and reconciliation are identical for every target."""

from __future__ import annotations

from executor import brevo, listmonk
from executor.target import OfflineTarget, TargetClient, TargetError

TARGETS = ("listmonk", "brevo")


def client_for_target(target: str, need_writes: bool) -> TargetClient:
    if target == "listmonk":
        settings = listmonk.ListmonkSettings()
        if settings.configured:
            return listmonk.client_from_settings(settings)
    elif target == "brevo":
        brevo_settings = brevo.BrevoSettings()
        if brevo_settings.configured:
            return brevo.client_from_settings(brevo_settings)
    else:
        raise TargetError(f"unknown target {target!r}; known: {TARGETS}")
    if need_writes:
        raise TargetError(
            f"target {target!r} is not configured (see .env.example); "
            "--execute and reconciliation need credentials"
        )
    print(f"note: {target} not configured; diffing against an assumed-empty target\n")
    return OfflineTarget()
