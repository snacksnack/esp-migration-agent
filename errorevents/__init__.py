"""Structured error events for remote diagnosis (RC1-647). See events.py."""

from errorevents.events import (
    CONNECT_CLASS_ATTR,
    CONNECT_CLASSES,
    ErrorEvent,
    classify_connect_error,
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
    "CONNECT_CLASS_ATTR",
    "CONNECT_CLASSES",
    "ErrorEvent",
    "classify_connect_error",
    "db_event",
    "emit",
    "emits_db_events",
    "http_event",
    "network_event",
    "new_correlation_id",
    "safe_route",
    "storage_event",
]
