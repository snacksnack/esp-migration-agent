# Structured error events (RC1-647, ADR-0026)

Runtime failures emit one JSON line each, safe to collect from a customer
environment and share with vendor triage: codes, enums, and opaque
identifiers only. Never exception message text, credentials, contact
fields, request bodies, or response bodies.

## Schema, version 1

Every event carries:

| Field | Always | Value |
|---|---|---|
| `event` | yes | `esp_error_event` - the marker a collector filters on |
| `v` | yes | schema version, `1` |
| `at` | yes | UTC timestamp, `YYYY-MM-DDTHH:MM:SSZ` |
| `correlation_id` | yes | opaque 16-hex token; a sink-failure event repeats the original's |
| `release` | yes | the package version of the running release |
| `op` | yes | operation ID from a registered set (below) |
| `error_class` | yes | `db`, `http`, `network`, or `storage` |
| `sqlstate` | db | five-character SQLSTATE (for example `42501`); malformed values are dropped |
| `status` | http, network | HTTP status code when one was seen |
| `route` | http, network | `METHOD /route/{id}` template, masked (below) |
| `provider_code` | http | the provider's error code, only when it is a documented enum value (Brevo); Listmonk has none |
| `errno` / `errno_name` | storage | numeric errno and symbolic name (`28` / `ENOSPC`); the filename OSError carries is never emitted |
| `kind` | network | `connect_error`, `ambiguous_outcome`, or `retries_exhausted` (the ADR-0015 taxonomy's verdict) |

Fields that do not apply are omitted (`exclude_none`).

**Operation IDs** are a closed set in `errorevents/events.py` (`OPERATIONS`):
`audit.append|read|verify`, `sessions.load|save|drop|revoke|claim`,
`scim.read|write`, `db.ready_check`, `destination.request`,
`journal.open|append|close`, `errorevents.sink`. Free-text labels (the
adapters' `what` strings name contacts and campaigns) can never appear.

**Route masking.** Routes are templates relative to the provider base URL:
any path segment that is not a plain word, an API version (`v3`), or a
`{placeholder}` is replaced with `{id}`, and query strings are stripped. The
mask is applied when the route is built and again by a schema validator, so
a concrete ID or a quoted email cannot pass through either way.

## Sink

- **Primary: stderr**, always. On the supported deployment profile that is
  the platform log stream, which the Phase 0B spike proved keeps working
  while the data volume is 100% full - the sink shares nothing with the
  storage fault it must report.
- **Optional file sink**: set `ESP_ERROR_EVENTS_PATH` to also append each
  event to a local file. The write flushes and closes per event - ENOSPC on
  a buffered write surfaces at flush or close, not at `write()` - and any
  failure there is treated as a write failure: the event already stands on
  stderr, and the failure becomes its own `errorevents.sink` event carrying
  the original's correlation ID.
- Emission never raises (an error event is evidence about a failure, not a
  second failure), and never phones home. Datadog telemetry remains a
  separate, optional, content-free channel.

## What a record does and does not establish

A record **localizes** a failure: which operation, which category, which
code. It does **not** by itself prove a root cause. A `403` on an upload
route may be an authorization failure at the provider or a proxy policy in
the customer's network; `SQLSTATE 42501` says a permission was missing, not
which grant was dropped or why. Triage output should treat records as
evidence for an investigation and, where the distinction matters, request
one specific follow-up check through the customer (install kit ADR 0002's
bundle-only channel) rather than conclude from the record alone.

## Relation to the execution journal

The journal (`docs/execution_journal.jsonl`, ADR-0016) is the operator's own
record and stays verbose - `run_failed` includes response text. It is not
share-safe. The error-event stream is the only diagnostic surface intended
to leave the customer environment, and the two are written independently:
a journal write failure is itself reported as an error event.
