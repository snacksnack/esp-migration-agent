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

from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from psycopg import errors
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from review_ui.audit import GENESIS, build_entry, log_entry, verify_records
from review_ui.oidc import Grant, SessionStore
from review_ui.scim import ScimDirectory, ScimUser, UserNameTaken, matches
from specs.model import ApproverIdentity

# pg_advisory_xact_lock key serializing chain appends: two writers can never
# both read the same tip and fork the chain (the seq primary key would refuse
# the second insert anyway; the lock makes it wait instead of fail).
CHAIN_LOCK = 554_576

COLUMNS = ("seq", "at", "event", "actor", "detail", "prev_sha256", "sha256")


class StoreNotReady(RuntimeError):
    """The database is reachable but not migrated (or the role lacks grants)."""


def open_pool(database_url: str) -> ConnectionPool:
    return ConnectionPool(
        database_url,
        min_size=0,
        max_size=4,
        max_idle=60,
        open=True,
        # Neon's pooled endpoints run PgBouncer in transaction mode, where
        # server-side prepared statements do not survive; harmless direct.
        kwargs={"prepare_threshold": None},
    )


def check_ready(pool: ConnectionPool) -> None:
    """Fail at startup, loudly, rather than on the first approval."""
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1 FROM review_audit LIMIT 0")
            conn.execute(
                "SELECT subject, email, revoked_at, revocation_audited"
                " FROM review_sso_sessions LIMIT 0"
            )
            conn.execute("SELECT 1 FROM review_scim_users LIMIT 0")
    except (errors.UndefinedTable, errors.InsufficientPrivilege) as exc:
        raise StoreNotReady(
            f"review-gate database is not ready ({exc.__class__.__name__}: {exc}); "
            "run `python -m review_ui.db_setup migrate` and `create-app-role`"
        ) from exc


def row_to_record(row: tuple) -> dict:
    record = dict(zip(COLUMNS, row, strict=True))
    record["prev_sha256"] = record["prev_sha256"].strip()
    record["sha256"] = record["sha256"].strip()
    return record


class PostgresAuditLog:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def record(self, event: str, actor: ApproverIdentity | None, **detail) -> dict:
        with self._pool.connection() as conn, conn.transaction():
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
        log_entry(entry)
        return entry

    def read(self, limit: int = 100) -> list[dict]:
        """Newest first."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM review_audit ORDER BY seq DESC LIMIT %s",
                (limit,),
            ).fetchall()
        return [row_to_record(r) for r in rows]

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

    def _drop(self, sid: str) -> Grant | None:
        grant = self._load(sid)
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM review_sso_sessions WHERE sid = %s", (sid,))
        return grant

    def _revoke(self, sid: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE review_sso_sessions SET revoked_at = now()"
                " WHERE sid = %s AND revoked_at IS NULL",
                (sid,),
            )

    def _revoke_owned(self, subjects: set[str], emails: set[str]) -> int:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE review_sso_sessions SET revoked_at = now()"
                " WHERE revoked_at IS NULL AND expires_at > now()"
                " AND (subject = ANY(%s) OR lower(email) = ANY(%s))",
                (list(subjects), list(emails)),
            )
            return cur.rowcount

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

    def get(self, user_id: str) -> ScimUser | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {SCIM_COLUMNS} FROM review_scim_users WHERE id = %s", (user_id,)
            ).fetchone()
        return _scim_user(row) if row else None

    def by_user_name(self, user_name: str) -> ScimUser | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {SCIM_COLUMNS} FROM review_scim_users"
                " WHERE lower(user_name) = lower(%s)",
                (user_name,),
            ).fetchone()
        return _scim_user(row) if row else None

    def page(self, start: int, count: int) -> tuple[int, list[ScimUser]]:
        with self._pool.connection() as conn:
            total = conn.execute("SELECT count(*) FROM review_scim_users").fetchone()[0]
            rows = conn.execute(
                f"SELECT {SCIM_COLUMNS} FROM review_scim_users"
                " ORDER BY created_at, id OFFSET %s LIMIT %s",
                (start - 1, count),
            ).fetchall()
        return total, [_scim_user(r) for r in rows]

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

    def delete(self, user_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM review_scim_users WHERE id = %s", (user_id,))

    def deprovisioned(self, identity: ApproverIdentity) -> ScimUser | None:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {SCIM_COLUMNS} FROM review_scim_users WHERE NOT active"
            ).fetchall()
        return next((u for u in map(_scim_user, rows) if matches(u, identity)), None)
