"""Postgres-backed review-gate state (RC1-576, ADR-0022).

Same interfaces as the file/memory stores (review_ui.audit.AuditLog,
review_ui.oidc.SessionStore), chosen by create_app when ESP_DATABASE_URL is
set. The app connects as the least-privilege role db_setup creates: INSERT
and SELECT on review_audit, nothing more, and the schema's triggers refuse
UPDATE/DELETE/TRUNCATE for every role besides. Refresh tokens are encrypted
with Fernet under a key the database never sees.

Connections come from a small pool that keeps none idle for long: Neon
scales the compute to zero between demo visits, and a held connection would
keep it awake.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from psycopg import Connection, OperationalError, errors
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout

from errorevents import (
    CONNECT_CLASS_ATTR,
    classify_connect_error,
    db_event,
    emit,
    emits_db_events,
)
from review_ui.audit import GENESIS, build_entry, log_entry, verify_records
from review_ui.oidc import AuditSink, Grant, SessionStore
from review_ui.scim import ScimDirectory, ScimUser, UserNameTaken, matches
from specs.model import ApproverIdentity

# pg_advisory_xact_lock key serializing chain appends: two writers can never
# both read the same tip and fork the chain (the seq primary key would refuse
# the second insert anyway; the lock makes it wait instead of fail).
CHAIN_LOCK = 554_576

COLUMNS = ("seq", "at", "event", "actor", "detail", "prev_sha256", "sha256")


class StoreNotReady(RuntimeError):
    """The database is reachable but not migrated (or the role lacks grants)."""


class ConnectOutcomes:
    """The class of the pool's most recent connect attempt (RC1-648).

    The pool connects on a background worker and retries with backoff; a
    caller waiting for a connection only ever sees PoolTimeout, with the
    worker's error logged as text and nowhere else. This keeps that error's
    class - never its message - so the timeout can say why. A success clears
    it: one later good connection means the earlier failure is not the cause.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._unresolved: str | None = None

    def failed(self, connect_class: str) -> None:
        with self._lock:
            self._unresolved = connect_class

    def succeeded(self) -> None:
        with self._lock:
            self._unresolved = None

    def unresolved_failure(self) -> str | None:
        with self._lock:
            return self._unresolved


def tracked_connection_class(outcomes: ConnectOutcomes) -> type[Connection]:
    """A Connection whose connect() records each attempt's outcome class."""

    class TrackedConnection(Connection):
        @classmethod
        def connect(cls, *args, **kwargs):
            try:
                conn = super().connect(*args, **kwargs)
            except OperationalError as exc:
                outcomes.failed(classify_connect_error(exc))
                raise
            outcomes.succeeded()
            return conn

    return TrackedConnection


class ClassifiedPool(ConnectionPool):
    """A pool whose PoolTimeout carries why no connection came: the class of
    the last connect attempt if it failed and nothing has connected since,
    otherwise pool_exhausted (connections exist and every one is in use)."""

    def __init__(self, *args, **kwargs) -> None:
        self.connect_outcomes = ConnectOutcomes()
        super().__init__(
            *args, connection_class=tracked_connection_class(self.connect_outcomes), **kwargs
        )

    def getconn(self, timeout: float | None = None) -> Connection:
        try:
            return super().getconn(timeout)
        except PoolTimeout as exc:
            cause = self.connect_outcomes.unresolved_failure() or "pool_exhausted"
            setattr(exc, CONNECT_CLASS_ATTR, cause)
            raise


def open_pool(database_url: str, *, timeout: float = 30.0) -> ClassifiedPool:
    return ClassifiedPool(
        database_url,
        min_size=0,
        max_size=4,
        max_idle=60,
        timeout=timeout,
        open=True,
        # Neon's pooled endpoints run PgBouncer in transaction mode, where
        # server-side prepared statements do not survive; harmless direct.
        kwargs={"prepare_threshold": None},
    )


def check_ready(pool: ConnectionPool) -> None:
    """Fail at startup, loudly, rather than on the first approval. A pool
    that cannot connect at all (a rotated password, a wrong host) emits its
    connect class here before the process exits."""
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1 FROM review_audit LIMIT 0")
            conn.execute(
                "SELECT subject, email, revoked_at, revocation_audited"
                " FROM review_sso_sessions LIMIT 0"
            )
            conn.execute("SELECT 1 FROM review_scim_users LIMIT 0")
    except (errors.UndefinedTable, errors.InsufficientPrivilege) as exc:
        emit(db_event("db.ready_check", exc))
        raise StoreNotReady(
            f"review-gate database is not ready ({exc.__class__.__name__}: {exc}); "
            "run `python -m review_ui.db_setup migrate` and `create-app-role`"
        ) from exc
    except PoolTimeout as exc:
        emit(db_event("db.ready_check", exc))
        raise


def row_to_record(row: tuple) -> dict:
    record = dict(zip(COLUMNS, row, strict=True))
    record["prev_sha256"] = record["prev_sha256"].strip()
    record["sha256"] = record["sha256"].strip()
    return record


class PostgresAuditLog:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    # Decorated as well as record_within: the connection and the commit are
    # taken here, outside the inner operation, and a failure at either was
    # silent until RC1-648. Nested decorators emit once, innermost first.
    @emits_db_events("audit.append")
    def record(self, event: str, actor: ApproverIdentity | None, **detail) -> dict:
        with self._pool.connection() as conn, conn.transaction():
            entry = self.record_within(conn, event, actor, **detail)
        log_entry(entry)
        return entry

    @emits_db_events("audit.append")
    def record_within(
        self, conn: Connection, event: str, actor: ApproverIdentity | None, **detail
    ) -> dict:
        """Append inside the caller's transaction (RC1-583), so the record
        commits with whatever else that transaction decided - or rolls back
        with it, leaving the chain intact. The caller logs the returned entry
        after the commit."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (CHAIN_LOCK,))
        tip = conn.execute(
            "SELECT seq, sha256 FROM review_audit ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        seq, prev = (tip[0], tip[1].strip()) if tip else (0, GENESIS)
        entry = build_entry(seq + 1, prev, event, actor, detail)
        conn.execute(
            "INSERT INTO review_audit (seq, at, event, actor, detail, prev_sha256, sha256)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                entry["seq"], entry["at"], entry["event"], Jsonb(entry["actor"]),
                Jsonb(entry["detail"]), entry["prev_sha256"], entry["sha256"],
            ),
        )
        return entry

    @emits_db_events("audit.read")
    def read(self, limit: int = 100) -> list[dict]:
        """Newest first."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM review_audit ORDER BY seq DESC LIMIT %s",
                (limit,),
            ).fetchall()
        return [row_to_record(r) for r in rows]

    @emits_db_events("audit.verify")
    def verify(self) -> list[str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM review_audit ORDER BY seq"
            ).fetchall()
        return verify_records((row_to_record(r) for r in rows), "seq")


class PostgresSessionStore(SessionStore):
    """SSO grants in review_sso_sessions, refresh tokens Fernet-encrypted.
    A row whose token no longer decrypts (key rotated) reads as absent:
    the user signs in again, never a crash or a trusted stale grant."""

    def __init__(self, pool: ConnectionPool, token_key: str):
        super().__init__()
        self._pool = pool
        self._fernet = Fernet(token_key.encode())

    @emits_db_events("sessions.load")
    def _load(self, sid: str) -> Grant | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT refresh_token_enc, checked_at, subject, email,"
                " revoked_at IS NOT NULL, revocation_audited FROM review_sso_sessions"
                " WHERE sid = %s AND expires_at > now()",
                (sid,),
            ).fetchone()
        if row is None:
            return None
        if row[4]:  # revoked: the token is never used again, so never decrypted
            return Grant("", row[1].timestamp(), row[2], row[3], revoked=True, audited=row[5])
        try:
            token = self._fernet.decrypt(row[0].encode()).decode()
        except InvalidToken:
            return None
        return Grant(token, row[1].timestamp(), row[2], row[3])

    @emits_db_events("sessions.save")
    def _save(self, sid: str, grant: Grant, ttl_seconds: int | None) -> bool:
        encrypted = self._fernet.encrypt(grant.refresh_token.encode()).decode()
        checked_at = datetime.fromtimestamp(grant.checked_at, UTC)
        with self._pool.connection() as conn, conn.transaction():
            if ttl_seconds is None:  # rotation: keep the session's expiry
                cur = conn.execute(
                    "UPDATE review_sso_sessions SET refresh_token_enc = %s, checked_at = %s"
                    " WHERE sid = %s AND revoked_at IS NULL",
                    (encrypted, checked_at, sid),
                )
                # 0 rows: revoked or dropped mid-refresh, so the rotation is
                # not ours to keep and check() must fail closed (RC1-582).
                return cur.rowcount == 1
            conn.execute("DELETE FROM review_sso_sessions WHERE expires_at <= now()")
            conn.execute(
                "INSERT INTO review_sso_sessions"
                " (sid, refresh_token_enc, checked_at, expires_at, subject, email)"
                " VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (sid) DO UPDATE SET refresh_token_enc = EXCLUDED.refresh_token_enc,"
                " checked_at = EXCLUDED.checked_at, expires_at = EXCLUDED.expires_at,"
                " subject = EXCLUDED.subject, email = EXCLUDED.email,"
                " revoked_at = NULL, revocation_audited = false",
                (
                    sid, encrypted, checked_at,
                    datetime.now(UTC) + timedelta(seconds=ttl_seconds),
                    grant.subject, grant.email,
                ),
            )
        return True

    @emits_db_events("sessions.drop")
    def _drop(self, sid: str) -> Grant | None:
        grant = self._load(sid)
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM review_sso_sessions WHERE sid = %s", (sid,))
        return grant

    @emits_db_events("sessions.revoke")
    def _revoke(self, sid: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE review_sso_sessions SET revoked_at = now()"
                " WHERE sid = %s AND revoked_at IS NULL",
                (sid,),
            )

    @emits_db_events("sessions.revoke")
    def _revoke_owned(self, subjects: set[str], emails: set[str]) -> int:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE review_sso_sessions SET revoked_at = now()"
                " WHERE revoked_at IS NULL AND expires_at > now()"
                " AND (subject = ANY(%s) OR lower(email) = ANY(%s))",
                (list(subjects), list(emails)),
            )
            return cur.rowcount

    @emits_db_events("sessions.claim")
    def claim_and_record(
        self,
        sid: str,
        audit: AuditSink,
        event: str,
        actor: ApproverIdentity | None,
        **detail,
    ) -> bool:
        """The claim and the record commit together (RC1-583): one
        transaction, so a failed insert rolls the claim back and the next
        request records the event instead of finding it already claimed."""
        if not isinstance(audit, PostgresAuditLog):
            return super().claim_and_record(sid, audit, event, actor, **detail)
        with self._pool.connection() as conn, conn.transaction():
            cur = conn.execute(
                "UPDATE review_sso_sessions SET revocation_audited = true"
                " WHERE sid = %s AND revoked_at IS NOT NULL AND NOT revocation_audited",
                (sid,),
            )
            if cur.rowcount != 1:
                return False
            entry = audit.record_within(conn, event, actor, **detail)
        log_entry(entry)
        return True

    @emits_db_events("sessions.claim")
    def _claim(self, sid: str) -> bool:
        # The row lock makes this the arbiter: of concurrent claimants,
        # exactly one sees rowcount 1.
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE review_sso_sessions SET revocation_audited = true"
                " WHERE sid = %s AND revoked_at IS NOT NULL AND NOT revocation_audited",
                (sid,),
            )
            return cur.rowcount == 1


SCIM_COLUMNS = "id, user_name, active, attributes, created_at, updated_at"


def _scim_user(row: tuple) -> ScimUser:
    return ScimUser(
        id=row[0], user_name=row[1], active=row[2], attributes=row[3],
        created=row[4], updated=row[5],
    )


class PostgresScimDirectory(ScimDirectory):
    """The SCIM directory in review_scim_users (RC1-575): the IdP keeps the
    ids this hands out, so they must outlive a deploy."""

    def __init__(self, pool: ConnectionPool):
        super().__init__()
        self._pool = pool

    @emits_db_events("scim.read")
    def get(self, user_id: str) -> ScimUser | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {SCIM_COLUMNS} FROM review_scim_users WHERE id = %s", (user_id,)
            ).fetchone()
        return _scim_user(row) if row else None

    @emits_db_events("scim.read")
    def by_user_name(self, user_name: str) -> ScimUser | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {SCIM_COLUMNS} FROM review_scim_users"
                " WHERE lower(user_name) = lower(%s)",
                (user_name,),
            ).fetchone()
        return _scim_user(row) if row else None

    @emits_db_events("scim.read")
    def page(self, start: int, count: int) -> tuple[int, list[ScimUser]]:
        with self._pool.connection() as conn:
            total = conn.execute("SELECT count(*) FROM review_scim_users").fetchone()[0]
            rows = conn.execute(
                f"SELECT {SCIM_COLUMNS} FROM review_scim_users"
                " ORDER BY created_at, id OFFSET %s LIMIT %s",
                (start - 1, count),
            ).fetchall()
        return total, [_scim_user(r) for r in rows]

    @emits_db_events("scim.write")
    def insert(self, user: ScimUser) -> None:
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    f"INSERT INTO review_scim_users ({SCIM_COLUMNS})"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (user.id, user.user_name, user.active, Jsonb(user.attributes),
                     user.created, user.updated),
                )
        except errors.UniqueViolation as exc:
            raise UserNameTaken(user.user_name) from exc

    @emits_db_events("scim.write")
    def update(self, user: ScimUser) -> None:
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    "UPDATE review_scim_users SET user_name = %s, active = %s,"
                    " attributes = %s, updated_at = %s WHERE id = %s",
                    (user.user_name, user.active, Jsonb(user.attributes), user.updated,
                     user.id),
                )
        except errors.UniqueViolation as exc:
            raise UserNameTaken(user.user_name) from exc

    @emits_db_events("scim.write")
    def delete(self, user_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM review_scim_users WHERE id = %s", (user_id,))

    @emits_db_events("scim.read")
    def deprovisioned(self, identity: ApproverIdentity) -> ScimUser | None:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {SCIM_COLUMNS} FROM review_scim_users WHERE NOT active"
            ).fetchall()
        return next((u for u in map(_scim_user, rows) if matches(u, identity)), None)
