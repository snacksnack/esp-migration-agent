"""Structured error events for remote diagnosis (RC1-647). See events.py."""

from errorevents.events import (
    ErrorEvent,
    db_event,
    emit,
    emits_db_events,
    http_event,
    network_event,
    new_correlation_id,
    safe_route,
    storage_event,
)

__all__ = [
    "ErrorEvent",
    "db_event",
    "emit",
    "emits_db_events",
    "http_event",
    "network_event",
    "new_correlation_id",
    "safe_route",
    "storage_event",
]
