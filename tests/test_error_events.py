"""RC1-647: the structured error-event contract.

Two properties carry the story: planted secrets (emails, API keys, contact
values, filenames) never reach an emitted record, and the sink survives the
storage fault - a file sink that fails at open, flush, or close still leaves
the event on stderr, with the sink failure recorded as its own event.
"""

from __future__ import annotations

import errno
import io
import json

import httpx
import pytest
from psycopg_pool import PoolTimeout

import errorevents.events as events
from errorevents import (
    CONNECT_CLASS_ATTR,
    CONNECT_CLASSES,
    classify_connect_error,
    db_event,
    emit,
    emits_db_events,
    http_event,
    network_event,
    safe_route,
    storage_event,
)
from executor.brevo import BrevoClient
from executor.journal import ExecutionJournal
from executor.listmonk import ListmonkClient
from executor.target import TargetError
from review_ui.pgstore import ConnectOutcomes, open_pool

PLANTED = (
    "alice@example.com",
    "xkeysib-0123456789abcdefSECRET",
    "Alice Smith",
    "row contains",
)


class FakePgError(Exception):
    """psycopg-shaped: a sqlstate attribute and a leaky message."""

    def __init__(self, sqlstate: str):
        super().__init__(
            "permission denied for table review_audit: "
            "row contains (alice@example.com, Alice Smith)"
        )
        self.sqlstate = sqlstate


def stderr_events(capsys) -> list[dict]:
    err = capsys.readouterr().err
    return [json.loads(line) for line in err.splitlines() if line.startswith("{")]


def assert_clean(capsys_text: str) -> None:
    for secret in PLANTED:
        assert secret not in capsys_text


# --- schema ---------------------------------------------------------------------


def test_db_event_carries_codes_only():
    event = db_event("audit.append", FakePgError("42501"))
    assert event.v == 2
    assert event.error_class == "db"
    assert event.sqlstate == "42501"
    assert event.release
    assert len(event.correlation_id) == 16
    line = event.model_dump_json(exclude_none=True)
    assert_clean(line)


def test_malformed_sqlstate_is_dropped_not_passed_through():
    assert db_event("audit.append", FakePgError("oops; see alice@example.com")).sqlstate is None


def test_unregistered_operation_refuses():
    with pytest.raises(ValueError, match="unregistered operation"):
        db_event("made.up", FakePgError("42501"))


def test_route_mask_strips_ids_emails_and_queries():
    assert safe_route("POST", "/contacts") == "POST /contacts"
    assert safe_route("put", "/contacts/42?email=alice@example.com") == "PUT /contacts/{id}"
    assert safe_route("GET", "/contacts/alice%40example.com") == "GET /contacts/{id}"
    assert (
        safe_route("POST", "/contacts/lists/{list_id}/contacts/add")
        == "POST /contacts/lists/{list_id}/contacts/add"
    )
    assert safe_route("POST", "/contacts/attributes/normal/zip4") == (
        "POST /contacts/attributes/normal/{id}"
    )


def test_event_validator_remasks_route():
    event = http_event(401, "POST /v3/contacts/7")
    assert event.route == "POST /v3/contacts/{id}"


def test_unknown_provider_code_is_dropped():
    assert http_event(401, "POST /contacts", provider_code="weird_new_code").provider_code is None
    assert http_event(401, "POST /contacts", provider_code="unauthorized").provider_code == (
        "unauthorized"
    )


def test_storage_event_takes_errno_never_filename():
    exc = OSError(errno.ENOSPC, "No space left on device", "/data/alice@example.com.json")
    event = storage_event("journal.append", exc)
    assert event.errno == errno.ENOSPC
    assert event.errno_name == "ENOSPC"
    assert_clean(event.model_dump_json(exclude_none=True))


def test_network_event_kind_is_closed():
    with pytest.raises(ValueError, match="unknown network kind"):
        network_event("dns_sadness", "GET /contacts")


# --- connect classes (RC1-648) -------------------------------------------------

# libpq/psycopg messages as measured against postgres:18 on 2026-11-12, with
# planted secrets where real messages carry names. None carries a SQLSTATE.
MEASURED = {
    "auth": 'connection to server at "127.0.0.1", port 55432 failed: FATAL:  password '
    'authentication failed for user "alice@example.com"',
    "database": 'connection to server at "127.0.0.1", port 55432 failed: FATAL:  database '
    '"xkeysib-0123456789abcdefSECRET" does not exist',
    "dns": "failed to resolve host 'alice@example.com': [Errno 8] nodename nor servname "
    "provided, or not known",
    "tls": 'connection to server at "127.0.0.1", port 55432 failed: server does not support '
    "SSL, but SSL was required",
    "timeout": "connection timeout expired",
    "network": 'connection to server at "127.0.0.1", port 1 failed: could not receive data '
    "from server: Connection refused",
    "server": 'connection to server at "127.0.0.1" failed: FATAL:  sorry, too many clients '
    "already",
    "unclassified": "Row contains Alice Smith: something nobody has seen before",
}


class FakeConnectError(Exception):
    """psycopg-shaped connect failure: sqlstate is None, the message leaks."""

    sqlstate = None


@pytest.mark.parametrize(("expected", "message"), sorted(MEASURED.items()))
def test_each_connect_class_from_its_measured_message(expected, message):
    assert classify_connect_error(FakeConnectError(message)) == expected


def test_every_class_but_pool_exhausted_has_a_measured_case():
    assert set(MEASURED) == CONNECT_CLASSES - {"pool_exhausted"}


def test_auth_wins_over_names_that_look_like_other_classes():
    # A role or database named after another class must not change the class.
    message = 'FATAL:  password authentication failed for user "ssl_timeout_reader"'
    assert classify_connect_error(FakeConnectError(message)) == "auth"


def test_sqlstate_beats_the_message_when_the_driver_has_one():
    exc = FakeConnectError("could not receive data from server")
    exc.sqlstate = "28P01"
    assert classify_connect_error(exc) == "auth"
    exc.sqlstate = "3D000"
    assert classify_connect_error(exc) == "database"
    exc.sqlstate = "53300"
    assert classify_connect_error(exc) == "server"


@pytest.mark.parametrize("message", sorted(MEASURED.values()))
def test_connect_event_carries_class_never_text(message, capsys):
    exc = FakeConnectError(message)
    setattr(exc, CONNECT_CLASS_ATTR, classify_connect_error(exc))
    emit(db_event("sessions.load", exc))
    (record,) = stderr_events(capsys)
    assert record["connect_class"] in CONNECT_CLASSES
    assert "sqlstate" not in record
    assert_clean(json.dumps(record))
    for word in ("127.0.0.1", "FATAL", "SSL", "failed"):
        assert word not in json.dumps(record)


def test_untagged_error_gets_no_connect_class():
    # A mid-query error without SQLSTATE is not a connect failure; db_event
    # never guesses one from the text.
    assert db_event("audit.read", FakeConnectError(MEASURED["network"])).connect_class is None


def test_unknown_connect_class_refuses():
    exc = FakeConnectError("x")
    setattr(exc, CONNECT_CLASS_ATTR, "alice@example.com")
    with pytest.raises(ValueError, match="unknown connect class"):
        db_event("sessions.load", exc)


def test_connect_outcomes_success_clears_the_failure():
    outcomes = ConnectOutcomes()
    assert outcomes.unresolved_failure() is None
    outcomes.failed("auth")
    assert outcomes.unresolved_failure() == "auth"
    outcomes.succeeded()
    assert outcomes.unresolved_failure() is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://u:pw-SECRET@nosuchhost.invalid:5432/db?connect_timeout=2", "dns"),
        ("postgresql://u:pw-SECRET@127.0.0.1:1/db?connect_timeout=2", "network"),
    ],
)
def test_pool_timeout_carries_the_real_connect_class(url, expected, capsys):
    # Real libpq, no server needed: the background worker's failure reaches
    # the waiting caller as a class on the PoolTimeout and in its event.
    pool = open_pool(url, timeout=2)
    try:
        with pytest.raises(PoolTimeout) as caught, pool.connection():
            pass
    finally:
        pool.close()
    assert getattr(caught.value, CONNECT_CLASS_ATTR) == expected
    emit(db_event("sessions.load", caught.value))
    (record,) = stderr_events(capsys)
    assert record["connect_class"] == expected
    assert "pw-SECRET" not in json.dumps(record)


# --- sink -----------------------------------------------------------------------


def test_emit_writes_one_json_line_to_stderr(capsys):
    emit(db_event("audit.append", FakePgError("42501")))
    (record,) = stderr_events(capsys)
    assert record["event"] == "esp_error_event"
    assert record["sqlstate"] == "42501"
    assert "sqlstate" in record and "message" not in record


def test_file_sink_is_additive(tmp_path, monkeypatch, capsys):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("ESP_ERROR_EVENTS_PATH", str(path))
    emit(db_event("audit.append", FakePgError("42501")))
    assert len(stderr_events(capsys)) == 1
    (line,) = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["sqlstate"] == "42501"


class FullDiskFile(io.StringIO):
    """Buffered-looking file: write succeeds, flush raises - the exact shape
    the Phase 0B spike measured on a 100%-full volume."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def flush(self):
        raise OSError(errno.ENOSPC, "No space left on device")


def test_sink_flush_failure_falls_back_to_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ESP_ERROR_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(events, "_open_sink", lambda *a, **k: FullDiskFile())
    emit(db_event("audit.append", FakePgError("42501")))  # must not raise
    records = stderr_events(capsys)
    assert [r.get("sqlstate", r.get("errno_name")) for r in records] == ["42501", "ENOSPC"]
    original, sink_failure = records
    assert sink_failure["op"] == "errorevents.sink"
    assert sink_failure["correlation_id"] == original["correlation_id"]


def test_sink_open_failure_falls_back_to_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ESP_ERROR_EVENTS_PATH", str(tmp_path))  # a directory: open fails
    emit(db_event("audit.append", FakePgError("42501")))
    records = stderr_events(capsys)
    assert records[0]["sqlstate"] == "42501"
    assert records[1]["op"] == "errorevents.sink"


# --- decorator ------------------------------------------------------------------


def test_decorator_emits_for_sqlstate_errors_only(capsys):
    @emits_db_events("sessions.save")
    def failing():
        raise FakePgError("42501")

    @emits_db_events("sessions.save")
    def domain_outcome():
        raise ValueError("user name taken")  # already converted: no sqlstate

    with pytest.raises(FakePgError):
        failing()
    with pytest.raises(ValueError):
        domain_outcome()
    records = stderr_events(capsys)
    assert len(records) == 1
    assert records[0]["op"] == "sessions.save"
    assert_clean(json.dumps(records))


def test_nested_decorators_emit_once_at_the_innermost_op(capsys):
    @emits_db_events("audit.append")
    def inner():
        raise FakePgError("42501")

    @emits_db_events("sessions.claim")
    def outer():
        inner()

    with pytest.raises(FakePgError):
        outer()
    (record,) = stderr_events(capsys)
    assert record["op"] == "audit.append"


# --- journal (storage family) ---------------------------------------------------


def test_journal_flush_failure_emits_storage_event_and_raises(tmp_path, capsys):
    journal = ExecutionJournal(tmp_path / "journal.jsonl")
    journal._fh = FullDiskFile()
    with pytest.raises(OSError, match="No space left"):
        journal.run_failed("boom")
    (record,) = stderr_events(capsys)
    assert record["error_class"] == "storage"
    assert record["op"] == "journal.append"
    assert record["errno_name"] == "ENOSPC"


def test_journal_open_failure_emits_and_raises(tmp_path, capsys):
    target = tmp_path / "taken"
    target.mkdir()
    with pytest.raises(OSError):
        ExecutionJournal(target)  # opening a directory fails
    (record,) = stderr_events(capsys)
    assert record["op"] == "journal.open"


# --- adapters (destination API family) ------------------------------------------


def brevo_client(handler) -> BrevoClient:
    return BrevoClient(
        "xkeysib-0123456789abcdefSECRET",
        transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
    )


def test_brevo_denied_endpoint_emits_status_route_and_code(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"code": "unauthorized", "message": "Key not found for alice@example.com"},
        )

    client = brevo_client(handler)
    with pytest.raises(TargetError, match="HTTP 401"):
        client._call("POST", "/contacts", "create contact alice@example.com")
    (record,) = stderr_events(capsys)
    assert record["error_class"] == "http"
    assert record["status"] == 401
    assert record["route"] == "POST /contacts"
    assert record["provider_code"] == "unauthorized"
    assert "Key not found" not in json.dumps(record)
    assert_clean(json.dumps(record))


def test_brevo_retries_exhausted_emits_network_event(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = brevo_client(handler)
    with pytest.raises(TargetError, match="after 4 attempts"):
        client._call("GET", "/contacts", "list all contacts")
    (record,) = stderr_events(capsys)
    assert record["error_class"] == "network"
    assert record["kind"] == "retries_exhausted"
    assert record["status"] == 503


def test_brevo_interpolated_path_without_template_is_masked(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": "document_not_found", "message": "no contact"})

    client = brevo_client(handler)
    with pytest.raises(TargetError):
        client._call("GET", "/contacts/alice%40example.com", "find contact")
    (record,) = stderr_events(capsys)
    assert record["route"] == "GET /contacts/{id}"


def test_listmonk_error_emits_without_provider_code(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "permission denied for alice@example.com"})

    client = ListmonkClient(
        "http://localhost:9000",
        "api_user",
        "sup3r-secret-token",
        transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
    )
    with pytest.raises(TargetError, match="HTTP 403"):
        client._call("PUT", "/subscribers/42", "update subscriber", route="/subscribers/{id}")
    (record,) = stderr_events(capsys)
    assert record["status"] == 403
    assert record["route"] == "PUT /subscribers/{id}"
    assert "provider_code" not in record
    assert "sup3r-secret-token" not in json.dumps(record)
    assert_clean(json.dumps(record))


def test_emitting_leaves_journal_contract_alone(tmp_path):
    # The share-safe stream and the operator's journal are different records:
    # the journal still gets its verbose line, the event stream never does.
    path = tmp_path / "journal.jsonl"
    journal = ExecutionJournal(path)
    journal.run_failed("HTTP 401 Key not found for alice@example.com")
    journal.close()
    assert "alice@example.com" in path.read_text(encoding="utf-8")
