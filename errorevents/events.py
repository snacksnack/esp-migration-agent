"""Structured error events for remote diagnosis (RC1-647, ADR-0026).

A runtime failure produces one JSON line built only from closed vocabularies
and mechanical codes: operation ID, error class, SQLSTATE, HTTP status and
route template, errno, an opaque correlation ID, release version, timestamp.
Never exception message text, credentials, contact fields, or request or
response bodies - Postgres names tables and row values in its messages, Brevo
echoes emails and API keys back, and either would turn a diagnostic record a
customer shares into a data leak.

The primary sink is the process stderr stream: on the supported deployment
profile that is the platform log stream, which the Phase 0B spike proved
survives a full data volume (stdout kept logging at 100% disk). An optional
additive file sink (ESP_ERROR_EVENTS_PATH) flushes and closes per event -
ENOSPC on a buffered write surfaces at flush or close, not at write() - and
any failure there is treated as a write failure: the event already stands on
stderr, and the sink failure becomes its own event. Emission never raises; an
error event is evidence about a failure, not a second failure.

These records support investigation; they do not by themselves prove a root
cause (see docs/error_events.md).
"""

from __future__ import annotations

import errno as errno_codes
import functools
import os
import re
import secrets
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, field_validator

from telemetry.llmobs import package_version

# Bumped on any change to the field set or a closed vocabulary (see
# docs/error_events.md, "Versioning"). v2 (RC1-648): connect_class.
SCHEMA_VERSION = 2
EVENT_NAME = "esp_error_event"

# The operations that may appear in an event: a closed set, so a record never
# carries a free-text label (adapter `what` strings name contacts and
# campaigns). Register new operations here when wiring new call sites.
OPERATIONS = frozenset(
    {
        "db.ready_check",
        "audit.append",
        "audit.read",
        "audit.verify",
        "sessions.load",
        "sessions.save",
        "sessions.drop",
        "sessions.revoke",
        "sessions.claim",
        "scim.read",
        "scim.write",
        "destination.request",
        "journal.open",
        "journal.append",
        "journal.close",
        "errorevents.sink",
    }
)

ERROR_CLASSES = frozenset({"db", "http", "network", "storage"})

# How an HTTP exchange failed without a status: the retry taxonomy's verdicts
# (ADR-0015), not transport exception text.
NETWORK_KINDS = frozenset({"connect_error", "ambiguous_outcome", "retries_exhausted"})

# Brevo's documented error-code enum (a closed set the provider publishes).
# An unknown code is dropped, never passed through: the field is an enum or
# it is nothing. Listmonk has no code field; its events carry status only.
PROVIDER_CODES = frozenset(
    {
        "unauthorized",
        "bad_request",
        "invalid_parameter",
        "missing_parameter",
        "out_of_range",
        "not_found",
        "method_not_allowed",
        "duplicate_parameter",
        "duplicate_request",
        "document_not_found",
        "account_under_validation",
        "permission_denied",
        "not_enough_credits",
        "unprocessable_entity",
    }
)

# Why a database connection could not be had (RC1-648). psycopg attaches no
# SQLSTATE to errors raised while connecting, auth failures included, so the
# class is derived in-process from libpq's message - which names roles,
# databases and hosts and is never emitted. `pool_exhausted` is the one class
# that is not a connect failure: every pooled connection was checked out and
# the last connect attempt had not failed. `unclassified` is an honest "the
# message matched nothing", never folded into `network`.
CONNECT_CLASSES = frozenset(
    {
        "auth",
        "database",
        "dns",
        "tls",
        "timeout",
        "network",
        "server",
        "pool_exhausted",
        "unclassified",
    }
)

# Order matters: the first match wins, and the auth and database messages
# quote names (a role or database called "ssl_reader" must not read as tls).
# Messages measured against postgres:18 and Neon, 2026-11-12.
_CONNECT_PATTERNS = (
    ("auth", ("password authentication failed", "no pg_hba.conf entry", "role \"",
              "authentication failed")),
    ("database", ("database \"",)),
    ("dns", ("failed to resolve host", "could not translate host name",
             "nodename nor servname", "name or service not known",
             "temporary failure in name resolution")),
    ("tls", ("ssl", "certificate", "tls")),
    ("timeout", ("timeout expired", "timed out")),
    ("server", ("too many clients", "remaining connection slots", "the database system is",
                "too many connections")),
    ("network", ("connection refused", "could not receive data", "no route to host",
                 "network is unreachable", "connection reset", "server closed the connection",
                 "could not connect", "host is unreachable")),
)


def classify_connect_error(exc: BaseException) -> str:
    """The closed class of a failed connection attempt. Reads the SQLSTATE
    when the driver provides one, otherwise matches libpq's message; returns
    a CONNECT_CLASSES member and nothing derived from the text itself."""
    state = getattr(exc, "sqlstate", None) or ""
    if state.startswith("28"):
        return "auth"
    if state.startswith("3D"):
        return "database"
    if state:
        return "server"
    text = str(exc).lower()
    for connect_class, needles in _CONNECT_PATTERNS:
        if any(needle in text for needle in needles):
            return connect_class
    return "unclassified"


METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})

# A route segment that is a plain word, an API version (v3), or a
# {placeholder} may appear in a record; anything else (numeric IDs, quoted
# emails, attribute names with digits) is masked. Applied twice: when a route
# is built and again by the model validator, so no caller can smuggle a
# concrete value past the mask.
_SAFE_SEGMENT = re.compile(r"^([A-Za-z_-]+|v\d{1,3}|\{[A-Za-z_]+\})$")
_SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")


def mask_path(path: str) -> str:
    """The path with every non-template segment replaced by {id}, query and
    fragment stripped."""
    bare = path.split("?", 1)[0].split("#", 1)[0]
    segments = [
        seg if _SAFE_SEGMENT.match(seg) else "{id}" for seg in bare.split("/") if seg != ""
    ]
    return "/" + "/".join(segments)


def safe_route(method: str, path_template: str) -> str:
    """`METHOD /route/{id}` - the only shape a route field may take."""
    return f"{method.upper()} {mask_path(path_template)}"


def new_correlation_id() -> str:
    return secrets.token_hex(8)


@lru_cache(maxsize=1)
def _release() -> str:
    try:
        return package_version()
    except Exception:  # a record without a release beats no record
        return "unknown"


class ErrorEvent(BaseModel):
    """One diagnostic record. Every field is an enum member, a mechanical
    code, or an opaque identifier; there is nowhere to put message text."""

    model_config = ConfigDict(extra="forbid")

    event: str = EVENT_NAME
    v: int = SCHEMA_VERSION
    at: str
    correlation_id: str
    release: str
    op: str
    error_class: str
    sqlstate: str | None = None
    connect_class: str | None = None
    status: int | None = None
    route: str | None = None
    provider_code: str | None = None
    errno: int | None = None
    errno_name: str | None = None
    kind: str | None = None

    @field_validator("op")
    @classmethod
    def _known_op(cls, value: str) -> str:
        if value not in OPERATIONS:
            raise ValueError(f"unregistered operation ID {value!r}")
        return value

    @field_validator("error_class")
    @classmethod
    def _known_class(cls, value: str) -> str:
        if value not in ERROR_CLASSES:
            raise ValueError(f"unknown error class {value!r}")
        return value

    @field_validator("sqlstate")
    @classmethod
    def _sqlstate_shape(cls, value: str | None) -> str | None:
        # A malformed value cannot be a SQLSTATE, and whatever it is instead
        # has no business in a record: drop it rather than pass it through.
        return value if value is not None and _SQLSTATE.match(value) else None

    @field_validator("connect_class")
    @classmethod
    def _known_connect_class(cls, value: str | None) -> str | None:
        if value is not None and value not in CONNECT_CLASSES:
            raise ValueError(f"unknown connect class {value!r}")
        return value

    @field_validator("route")
    @classmethod
    def _route_masked(cls, value: str | None) -> str | None:
        if value is None:
            return None
        method, _, path = value.partition(" ")
        if method.upper() not in METHODS or not path.startswith("/"):
            raise ValueError(f"route must be 'METHOD /template', got {value!r}")
        return safe_route(method, path)

    @field_validator("provider_code")
    @classmethod
    def _known_provider_code(cls, value: str | None) -> str | None:
        return value if value in PROVIDER_CODES else None

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str | None) -> str | None:
        if value is not None and value not in NETWORK_KINDS:
            raise ValueError(f"unknown network kind {value!r}")
        return value


def _base(op: str, correlation_id: str | None) -> dict:
    return {
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "correlation_id": correlation_id or new_correlation_id(),
        "release": _release(),
        "op": op,
    }


# The attribute a connection source (review_ui.pgstore's pool) sets on an
# exception it knows to be a failure to obtain a connection.
CONNECT_CLASS_ATTR = "esp_connect_class"


def db_event(op: str, exc: BaseException, *, correlation_id: str | None = None) -> ErrorEvent:
    """A database-operation failure. SQLSTATE is read by duck typing (psycopg
    puts it on every Error) so this module imports no driver; the message,
    which names tables and values, is never read here. A connect class is
    present only when the connection source tagged the exception: an error
    without a SQLSTATE mid-query is not a connect failure, and guessing one
    from its text would put a wrong class in front of triage."""
    return ErrorEvent(
        error_class="db",
        sqlstate=getattr(exc, "sqlstate", None),
        connect_class=getattr(exc, CONNECT_CLASS_ATTR, None),
        **_base(op, correlation_id),
    )


def http_event(
    status: int,
    route: str,
    *,
    provider_code: str | None = None,
    op: str = "destination.request",
    correlation_id: str | None = None,
) -> ErrorEvent:
    """The server understood and refused: status, route template, and the
    provider's error code if it is one of the documented enum values."""
    return ErrorEvent(
        error_class="http",
        status=status,
        route=route,
        provider_code=provider_code,
        **_base(op, correlation_id),
    )


def network_event(
    kind: str,
    route: str,
    *,
    status: int | None = None,
    op: str = "destination.request",
    correlation_id: str | None = None,
) -> ErrorEvent:
    """No usable response: connect failure, ambiguous outcome, or retries
    exhausted - with the last seen status when there was one."""
    return ErrorEvent(
        error_class="network", kind=kind, status=status, route=route, **_base(op, correlation_id)
    )


def storage_event(op: str, exc: OSError, *, correlation_id: str | None = None) -> ErrorEvent:
    """A local write failed. errno and its symbolic name; never the filename,
    which OSError carries and which can embed user-derived path parts."""
    code = exc.errno
    return ErrorEvent(
        error_class="storage",
        errno=code,
        errno_name=errno_codes.errorcode.get(code) if code is not None else None,
        **_base(op, correlation_id),
    )


# Indirection so tests can stand in a file object whose flush or close fails
# the way a 100%-full volume does.
_open_sink: Callable = open


def _stderr(line: str) -> None:
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except (OSError, ValueError):  # a dead stderr leaves nothing better to do
        pass


def emit(event: ErrorEvent) -> None:
    """Write one event. stderr always (the platform log stream, which the
    Phase 0B spike proved independent of the data volume); additionally the
    ESP_ERROR_EVENTS_PATH file when configured. The file write flushes and
    closes before it counts - ENOSPC hides in buffers until then - and its
    failure becomes a sink event on stderr. Never raises."""
    try:
        line = event.model_dump_json(exclude_none=True)
    except Exception:
        return
    _stderr(line)
    path = os.environ.get("ESP_ERROR_EVENTS_PATH")
    if not path:
        return
    failure: OSError | None = None
    try:
        with _open_sink(path, "a", encoding="utf-8") as sink:
            sink.write(line + "\n")
            sink.flush()
    except OSError as exc:  # open, write, flush, or close
        failure = exc
    if failure is not None:
        _stderr(
            storage_event(
                "errorevents.sink", failure, correlation_id=event.correlation_id
            ).model_dump_json(exclude_none=True)
        )


def emits_db_events(op: str):
    """Decorator for store methods: a propagating exception that carries a
    SQLSTATE attribute (psycopg's whole Error family) emits a db event and
    propagates unchanged. Exceptions already converted to domain outcomes
    (UserNameTaken and the like) carry no sqlstate and emit nothing. Nested
    decorated calls emit once, at the innermost - most specific - operation."""

    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                if hasattr(exc, "sqlstate") and not getattr(exc, "_esp_event_emitted", False):
                    emit(db_event(op, exc))
                    exc._esp_event_emitted = True
                raise

        return wrapper

    return decorate
