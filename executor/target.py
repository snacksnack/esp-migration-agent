"""Target-client protocol (RC1-546): what the executor needs from any target.

The real Listmonk adapter (executor/listmonk.py) and the in-memory fake the
unit tests run against (tests/fake_target.py) implement the same protocol, so
idempotency, diffing, and the reconciliation math are tested without network.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from executor.plan import DesiredSubscriber, PlannedCampaign


class TargetSubscriber(BaseModel):
    """A subscriber as the target currently holds it (normalized)."""

    id: int
    email: str
    name: str
    status: str
    subscription: str
    attribs: dict[str, str | int]


class TargetError(RuntimeError):
    pass


class TargetClient(Protocol):
    def describe_destination(self) -> dict[str, str]:
        """Destination identity for the execution journal (RC1-571):
        at least {"platform": ...}, plus host/account where they vary."""
        ...

    def find_list(self, name: str) -> int | None:
        """Find the list WITHOUT creating it; None when absent. The only
        list lookup read paths (dry run, reconcile) may use (RC1-564)."""
        ...

    def ensure_list(self, name: str) -> int:
        """Find or create the list; returns its id. Write paths only."""
        ...

    def current_subscribers(self, list_id: int) -> dict[str, TargetSubscriber]:
        """Current target state, keyed by lowercased email."""
        ...

    def create_subscriber(self, desired: DesiredSubscriber, list_id: int) -> None: ...

    def update_subscriber(
        self, existing: TargetSubscriber, desired: DesiredSubscriber, list_id: int
    ) -> None: ...

    def current_campaigns(self) -> dict[str, PlannedCampaign]:
        """Existing campaigns keyed by name (body in target syntax)."""
        ...

    def create_campaign(self, campaign: PlannedCampaign, list_id: int) -> None: ...

    def update_campaign(self, name: str, campaign: PlannedCampaign, list_id: int) -> None: ...

    def count_query(self, list_id: int, query: str) -> int:
        """Subscriber count for a target-dialect segment query."""
        ...


class OfflineTarget:
    """Stand-in when no target is configured: reports an empty target so the
    dry-run diff renders offline, and refuses every write loudly."""

    def describe_destination(self) -> dict[str, str]:
        return {"platform": "offline"}

    def find_list(self, name: str) -> int | None:
        return None

    def ensure_list(self, name: str) -> int:
        return 0

    def current_subscribers(self, list_id: int) -> dict[str, TargetSubscriber]:
        return {}

    def current_campaigns(self) -> dict[str, PlannedCampaign]:
        return {}

    def _refuse(self, *_args, **_kwargs) -> None:
        raise TargetError(
            "no target configured (set ESP_LISTMONK_URL); dry-run is the only "
            "mode available offline"
        )

    create_subscriber = _refuse
    update_subscriber = _refuse
    create_campaign = _refuse
    update_campaign = _refuse
    count_query = _refuse
