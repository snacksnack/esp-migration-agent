"""In-memory TargetClient for the RC1-546 unit tests: same protocol as the
Listmonk adapter, so planning, diffing, execution, idempotency, and the
reconciliation math run without network. Counts queries with the same
evaluator reconciliation uses."""

from __future__ import annotations

from executor.plan import DesiredSubscriber, PlannedCampaign
from executor.queries import evaluate_query
from executor.target import TargetSubscriber


class FakeTarget:
    def __init__(self) -> None:
        self.lists: dict[str, int] = {}
        self.subscribers: dict[str, TargetSubscriber] = {}
        self.campaigns: dict[str, PlannedCampaign] = {}
        self.write_calls = 0
        self._next_id = 1

    def find_list(self, name: str) -> int | None:
        return self.lists.get(name)

    def ensure_list(self, name: str) -> int:
        if name not in self.lists:
            self.lists[name] = len(self.lists) + 1
        return self.lists[name]

    def current_subscribers(self, list_id: int) -> dict[str, TargetSubscriber]:
        return {k: v.model_copy(deep=True) for k, v in self.subscribers.items()}

    def _store(self, desired: DesiredSubscriber, sub_id: int) -> None:
        self.subscribers[desired.email.lower()] = TargetSubscriber(
            id=sub_id,
            email=desired.email,
            name=desired.name,
            status=desired.status,
            subscription=desired.subscription,
            attribs=dict(desired.attribs),
        )
        self.write_calls += 1

    def create_subscriber(self, desired: DesiredSubscriber, list_id: int) -> None:
        self._store(desired, self._next_id)
        self._next_id += 1

    def update_subscriber(
        self, existing: TargetSubscriber, desired: DesiredSubscriber, list_id: int
    ) -> None:
        self._store(desired, existing.id)

    def current_campaigns(self) -> dict[str, PlannedCampaign]:
        return {k: v.model_copy(deep=True) for k, v in self.campaigns.items()}

    def create_campaign(self, campaign: PlannedCampaign, list_id: int) -> None:
        self.campaigns[campaign.name] = campaign.model_copy(deep=True)
        self.write_calls += 1

    def update_campaign(self, name: str, campaign: PlannedCampaign, list_id: int) -> None:
        self.campaigns[name] = campaign.model_copy(deep=True)
        self.write_calls += 1

    def count_query(self, list_id: int, query: str) -> int:
        return sum(
            1 for s in self.subscribers.values() if evaluate_query(query, s.attribs)
        )
