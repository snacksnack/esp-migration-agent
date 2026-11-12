"""Durable review-gate state in Postgres (RC1-576, ADR-0022).

Runs against a real Postgres: ESP_TEST_DATABASE_URL must be a superuser URL
to a throwaway database (CI: a postgres:18 service container; locally:
`docker run -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:18`). Skipped
without it - read from the process env only, never .env.

Each test re-creates the schema and connects the app as the least-privilege
role db_setup creates, so every check runs under the production grants.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout

from mapping.gate import load_spec
from review_ui import auth, db_setup
from review_ui.api import create_app
from review_ui.config import ReviewUiSettings
from review_ui.db_setup import SetupError, create_app_role, migrate, schema_marker, verify
from review_ui.pgstore import (
    PostgresAuditLog,
    PostgresSessionStore,
    StoreNotReady,
    check_ready,
    open_pool,
)
from specs.model import ApproverIdentity
from tests.test_review_scim import OKTA, TOKEN, deactivate_patch, okta_user
from tests.test_review_sso import (
    AS_OF,
    AUDIENCE,
    DRAFT,
    SUPPLEMENT,
    FakeIdp,
    resolve_everything,
    sso_login,
)

ADMIN_URL = os.environ.get("ESP_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="ESP_TEST_DATABASE_URL not set")

TEST_ROLE = "esp_app_test"


def reset_schema() -> None:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS review_audit, review_sso_sessions, review_scim_users,"
            " review_schema_version"
        )


@pytest.fixture()
def db():
    """Fresh schema; yields the app role's URL."""
    reset_schema()
    migrate(ADMIN_URL)
    yield create_app_role(ADMIN_URL, TEST_ROLE)


@pytest.fixture()
def apps():
    """Builds apps and closes their pools afterward."""
    made = []

    def build(tmp_path: Path, idp: FakeIdp, app_url: str, key: str, **overrides):
        spec_path = tmp_path / DRAFT.name
        if not spec_path.exists():
            spec_path.write_text(DRAFT.read_text())
        values = {
            "review_password": "let-me-in",
            "review_session_secret": "test-secret",
            "oidc_issuer": "https://customer.okta.example",
            "oidc_client_id": "client",
            "oidc_client_secret": "secret",
            "oidc_recheck_seconds": 0,
            "audit_log": tmp_path / "unused.jsonl",
            "database_url": app_url,
            "session_token_key": key,
            "_env_file": None,
        }
        values.update(overrides)
        app = create_app(
            spec_path,
            audience_dir=AUDIENCE,
            supplement_dir=SUPPLEMENT,
            account_tz="America/New_York",
            target="listmonk",
            as_of=AS_OF,
            approved_dir=tmp_path / "approved",
            settings=ReviewUiSettings(**values),
            provider=idp,
        )
        made.append(app)
        return TestClient(app, follow_redirects=False)

    yield build
    for app in made:
        if app.state.pool is not None:
            app.state.pool.close()


def admin_rows(query: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(ADMIN_URL) as conn:
        return conn.execute(query, params).fetchall()


def events() -> list[str]:
    return [r[0] for r in admin_rows("SELECT event FROM review_audit ORDER BY seq")]


KEY = Fernet.generate_key().decode()


def test_migrate_is_idempotent_and_the_app_role_holds_only_its_grants(db):
    migrate(ADMIN_URL)  # second run: no error, nothing duplicated
    grants = admin_rows(
        "SELECT table_name, privilege_type FROM information_schema.role_table_grants"
        " WHERE grantee = %s ORDER BY 1, 2",
        (TEST_ROLE,),
    )
    assert grants == [
        ("review_audit", "INSERT"),
        ("review_audit", "SELECT"),
        ("review_scim_users", "DELETE"),
        ("review_scim_users", "INSERT"),
        ("review_scim_users", "SELECT"),
        ("review_scim_users", "UPDATE"),
        ("review_sso_sessions", "DELETE"),
        ("review_sso_sessions", "INSERT"),
        ("review_sso_sessions", "SELECT"),
        ("review_sso_sessions", "UPDATE"),
    ]


def test_the_database_refuses_to_rewrite_the_log_for_every_role(db):
    pool = open_pool(db)
    try:
        log = PostgresAuditLog(pool)
        log.record("sign_in", None)
        log.record("spec_approved", None, content_sha256="a" * 64)
    finally:
        pool.close()
    assert verify(db) == []  # as the app role: intact, and all three rewrites refused
    # Even the owner/superuser is stopped by the triggers.
    for attempt in ("UPDATE review_audit SET event = 'x'", "DELETE FROM review_audit",
                    "TRUNCATE review_audit"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):  # noqa: SIM117
            with psycopg.connect(ADMIN_URL) as conn:
                conn.execute(attempt)
    assert events() == ["sign_in", "spec_approved"]


def test_gate_decisions_land_in_postgres_attributed_and_chained(tmp_path, db, apps):
    client = apps(tmp_path, FakeIdp(), db, KEY)
    sso_login(client)
    resolve_everything(client)
    assert client.post("/api/approve", json={}).status_code == 200
    logged = events()
    assert logged[0] == "service_start"  # the spec reset is on the record
    assert logged[1] == "sign_in" and logged[-1] == "spec_approved"
    assert logged.count("mapping_resolved") == 16
    newest = client.get("/api/audit").json()["records"]
    assert newest[0]["event"] == "spec_approved"
    assert newest[0]["actor"]["subject"] == "00u-dana"
    assert verify(db) == []


def test_sso_sessions_and_the_chain_survive_a_restart(tmp_path, db, apps):
    idp = FakeIdp()  # the IdP outlives our process
    first = apps(tmp_path, idp, db, KEY)
    sso_login(first)
    cookie = first.cookies.get(auth.COOKIE_NAME)
    restarted = apps(tmp_path, idp, db, KEY)  # new process, same database
    restarted.cookies.set(auth.COOKIE_NAME, cookie)
    resp = restarted.patch(
        "/api/mappings/consent/0", json={"source": "subscribed", "status": "approved"}
    )
    assert resp.status_code == 200, resp.json()  # pre-RC1-576 this was a 401
    assert events() == ["service_start", "sign_in", "service_start", "mapping_resolved"]
    assert verify(db) == []  # one chain across both processes


def test_deactivation_still_ends_access_and_revokes_the_grant(tmp_path, db, apps):
    idp = FakeIdp()
    client = apps(tmp_path, idp, db, KEY)
    sso_login(client)
    assert admin_rows("SELECT count(*) FROM review_sso_sessions") == [(1,)]
    idp.active["dana"] = False
    resp = client.patch(
        "/api/mappings/consent/0", json={"source": "subscribed", "status": "approved"}
    )
    assert resp.status_code == 401
    assert admin_rows(
        "SELECT count(*) FROM review_sso_sessions WHERE revoked_at IS NULL"
    ) == [(0,)]
    assert events()[-1] == "access_revoked"


def test_refresh_tokens_are_encrypted_at_rest(tmp_path, db, apps):
    idp = FakeIdp()
    client = apps(tmp_path, idp, db, KEY)
    sso_login(client)
    (stored,) = admin_rows("SELECT refresh_token_enc FROM review_sso_sessions")[0]
    assert "rt-dana" not in stored
    # Rotating the key orphans stored grants: a fresh sign-in, never a crash.
    cookie = client.cookies.get(auth.COOKIE_NAME)
    rekeyed = apps(tmp_path, idp, db, Fernet.generate_key().decode())
    rekeyed.cookies.set(auth.COOKIE_NAME, cookie)
    resp = rekeyed.patch(
        "/api/mappings/consent/0", json={"source": "subscribed", "status": "approved"}
    )
    assert resp.status_code == 401


def test_rotation_keeps_the_session_expiry(tmp_path, db, apps):
    idp = FakeIdp()
    client = apps(tmp_path, idp, db, KEY)
    sso_login(client)
    before = admin_rows("SELECT expires_at, refresh_token_enc FROM review_sso_sessions")[0]
    resolve_everything(client)  # every write rotates the refresh token
    after = admin_rows("SELECT expires_at, refresh_token_enc FROM review_sso_sessions")[0]
    assert after[0] == before[0] and after[1] != before[1]


def test_expired_sessions_are_purged_on_the_next_sign_in(tmp_path, db, apps):
    client = apps(tmp_path, FakeIdp(), db, KEY)
    sso_login(client)
    with psycopg.connect(ADMIN_URL) as conn:
        conn.execute("UPDATE review_sso_sessions SET expires_at = now() - interval '1 second'")
    sso_login(client, "sam")
    assert admin_rows("SELECT count(*) FROM review_sso_sessions") == [(1,)]


def test_concurrent_writers_never_fork_the_chain(db):
    pool = open_pool(db)
    log = PostgresAuditLog(pool)
    try:
        threads = [
            threading.Thread(target=lambda i=i: log.record("mapping_resolved", None, i=i))
            for i in range(12)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert log.verify() == []
    finally:
        pool.close()
    assert [r[0] for r in admin_rows("SELECT seq FROM review_audit ORDER BY seq")] == list(
        range(1, 13)
    )


def test_misconfiguration_fails_at_startup(tmp_path, db, apps):
    with pytest.raises(ValueError, match="ESP_SESSION_TOKEN_KEY"):
        apps(tmp_path, FakeIdp(), db, None)
    reset_schema()  # reachable, but never migrated
    with pytest.raises(StoreNotReady, match="db_setup migrate"):
        apps(tmp_path, FakeIdp(), db, KEY)


def test_migrate_regrants_an_existing_app_role(db):
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f"REVOKE ALL ON review_scim_users FROM {TEST_ROLE}")
    assert migrate(ADMIN_URL, app_role=TEST_ROLE) is True
    grants = admin_rows(
        "SELECT privilege_type FROM information_schema.role_table_grants"
        " WHERE grantee = %s AND table_name = 'review_scim_users' ORDER BY 1",
        (TEST_ROLE,),
    )
    assert [g[0] for g in grants] == ["DELETE", "INSERT", "SELECT", "UPDATE"]


def test_scim_deactivation_ends_postgres_sessions_and_survives_a_restart(tmp_path, db, apps):
    idp = FakeIdp()
    first = apps(tmp_path, idp, db, KEY, scim_token=TOKEN, oidc_recheck_seconds=3600)
    created = first.post("/scim/v2/Users", json=okta_user("dana", external_id="00u-dana"),
                         headers=OKTA)
    assert created.status_code == 201
    dana_id = created.json()["id"]
    sso_login(first)
    sso_login(TestClient(first.app, follow_redirects=False), "sam")
    assert admin_rows("SELECT count(*) FROM review_sso_sessions") == [(2,)]
    # A deploy between provisioning and deactivation: Okta still holds the id.
    restarted = apps(tmp_path, idp, db, KEY, scim_token=TOKEN, oidc_recheck_seconds=3600)
    restarted.cookies.update(first.cookies)
    resp = restarted.patch(f"/scim/v2/Users/{dana_id}", json=deactivate_patch(), headers=OKTA)
    assert resp.status_code == 200 and resp.json()["active"] is False
    assert admin_rows(
        "SELECT email FROM review_sso_sessions WHERE revoked_at IS NULL"
    ) == [("sam@customer.example",)]
    dana_cookie = dict(restarted.cookies)
    assert restarted.get("/api/me").json()["revoked_by"] == "Okta"
    # A second request racing the first (same cookie) is told, not re-logged.
    late = TestClient(restarted.app, follow_redirects=False, cookies=dana_cookie)
    assert late.get("/api/me").json()["revoked_by"] == "Okta"
    assert idp.refresh_calls == 0  # pushed, not polled
    assert events()[-2:] == ["scim_deactivated", "access_revoked"]
    dup = restarted.post("/scim/v2/Users", json=okta_user("DANA"), headers=OKTA)
    assert dup.status_code == 409  # the unique index is case-insensitive
    assert sso_login(restarted).headers["location"].startswith("/?sso_error=")
    assert verify(db) == []


def test_revocation_during_a_refresh_does_not_authorize_the_write(tmp_path, db, apps):
    """RC1-582, against the real guard: the UPDATE carries
    `WHERE revoked_at IS NULL`, so a revocation landing mid-refresh matches no
    row and the write must be refused rather than allowed through once."""
    idp = FakeIdp()
    client = apps(tmp_path, idp, db, KEY)
    sso_login(client)
    answer = idp.refresh

    def revoke_mid_refresh(refresh_token: str) -> str | None:
        with psycopg.connect(ADMIN_URL) as conn:  # the SCIM push, mid-flight
            conn.execute(
                "UPDATE review_sso_sessions SET revoked_at = now()"
                " WHERE revoked_at IS NULL"
            )
        return answer(refresh_token)

    idp.refresh = revoke_mid_refresh
    resp = client.patch(
        "/api/mappings/consent/0", json={"source": "subscribed", "status": "approved"}
    )
    assert resp.status_code == 401
    assert load_spec(tmp_path / DRAFT.name).consent_mappings[0].status.value == "proposed"
    assert client.get("/api/me").json()["authenticated"] is False


def test_a_failed_revocation_record_rolls_the_claim_back(db):
    """RC1-583, on the real transaction: the claim and the audit insert commit
    together, so an insert that raises leaves the session unclaimed and the
    event still recordable by the next request."""
    pool = open_pool(db)
    try:
        sessions = PostgresSessionStore(pool, KEY)
        audit = PostgresAuditLog(pool)
        identity = ApproverIdentity(
            method="oidc",
            verified=True,
            issuer="https://customer.okta.example",
            subject="00u-dana",
            email="dana@customer.example",
            name="Dana Ops",
        )
        sessions.put("sid-dana", "rt-dana-1", 3600, identity)
        assert sessions.end_sessions({"00u-dana"}, set()) == 1

        class FailingInsert(PostgresAuditLog):
            """Inserts, then raises - still inside the caller's transaction."""

            def record_within(self, conn, event, actor, **detail):
                super().record_within(conn, event, actor, **detail)
                raise RuntimeError("audit insert failed")

        before = events()
        with pytest.raises(RuntimeError):
            sessions.claim_and_record(
                "sid-dana", FailingInsert(pool), "access_revoked", identity
            )
        assert events() == before  # rolled back with the claim
        assert admin_rows(
            "SELECT revocation_audited FROM review_sso_sessions WHERE sid = %s",
            ("sid-dana",),
        ) == [(False,)]
        # A later request records it, exactly once.
        assert sessions.claim_and_record("sid-dana", audit, "access_revoked", identity)
        assert events() == [*before, "access_revoked"]
        assert not sessions.claim_and_record("sid-dana", audit, "access_revoked", identity)
        assert events() == [*before, "access_revoked"]
    finally:
        pool.close()


def test_migrate_records_the_schema_marker_and_the_app_role_cannot_read_it(db):
    with psycopg.connect(ADMIN_URL) as conn:
        assert schema_marker(conn) == (db_setup.SCHEMA_VERSION, db_setup.SCHEMA_COMPATIBLE_WITH)
    with psycopg.connect(db) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT * FROM review_schema_version")


def test_migrate_refuses_a_database_newer_than_its_code(db, monkeypatch):
    newer = db_setup.SCHEMA_VERSION + 1
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(
            "UPDATE review_schema_version SET version = %s, compatible_with = %s", (newer, newer)
        )
    with pytest.raises(SetupError, match=f"schema is v{newer}"):
        migrate(ADMIN_URL)
    with psycopg.connect(ADMIN_URL) as conn:
        assert schema_marker(conn) == (newer, newer)  # untouched by the refused run


# --- RC1-648: connect failures carry a class, never text --------------------------


def with_db_url(url: str, *, password: str | None = None, database: str | None = None,
                query: str | None = None) -> str:
    parts = urlsplit(url)
    netloc = parts.netloc
    if password is not None:
        creds, _, host = netloc.rpartition("@")
        user = creds.split(":", 1)[0]
        netloc = f"{user}:{password}@{host}"
    path = f"/{database}" if database is not None else parts.path
    return urlunsplit((parts.scheme, netloc, path, query or parts.query, ""))


def error_events(capsys) -> list[dict]:
    err = capsys.readouterr().err
    return [json.loads(line) for line in err.splitlines() if line.startswith("{")]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"password": "wrong-pw-SECRET"}, "auth"),
        ({"database": "no_such_db_SECRET"}, "database"),
        ({"query": "sslmode=require"}, "tls"),  # the CI/dev server has no TLS
    ],
)
def test_startup_against_a_bad_url_emits_its_connect_class(db, change, expected, capsys):
    pool = open_pool(with_db_url(db, **change), timeout=3)
    try:
        with pytest.raises(PoolTimeout):
            check_ready(pool)
    finally:
        pool.close()
    (record,) = error_events(capsys)
    assert record["op"] == "db.ready_check"
    assert record["connect_class"] == expected
    assert "sqlstate" not in record
    line = json.dumps(record)
    for leak in ("SECRET", TEST_ROLE, "127.0.0.1", "localhost"):
        assert leak not in line


def test_a_rotated_app_password_reads_as_auth_on_a_running_app(db, capsys):
    pool = open_pool(db, timeout=3)
    try:
        log = PostgresAuditLog(pool)
        log.record("sign_in", None)  # healthy, with a pooled connection
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(f"ALTER ROLE {TEST_ROLE} PASSWORD 'rotated-elsewhere'")
            # What a Neon scale-to-zero does to every open session.
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s",
                (TEST_ROLE,),
            )
        # The pooled connection dies mid-use: a server error with a SQLSTATE
        # and no connect class. Then the pool must reconnect, and cannot.
        with pytest.raises(psycopg.OperationalError):
            log.record("sign_in", None)
        with pytest.raises(PoolTimeout):
            log.record("sign_in", None)
    finally:
        pool.close()
    dropped, refused = error_events(capsys)
    assert "connect_class" not in dropped
    assert refused["op"] == "audit.append"
    assert refused["connect_class"] == "auth"


def test_every_connection_in_use_reads_as_pool_exhausted(db, capsys):
    pool = open_pool(db, timeout=1)
    held = [pool.getconn() for _ in range(pool.max_size)]
    try:
        with pytest.raises(PoolTimeout):
            PostgresAuditLog(pool).record("sign_in", None)
    finally:
        for conn in held:
            pool.putconn(conn)
        pool.close()
    (record,) = error_events(capsys)
    assert record["connect_class"] == "pool_exhausted"


def test_a_later_good_connection_clears_the_failed_class(db):
    pool = open_pool(with_db_url(db, password="wrong"), timeout=2)
    try:
        with pytest.raises(PoolTimeout), pool.connection():
            pass
        assert pool.connect_outcomes.unresolved_failure() == "auth"
        pool.conninfo = db  # the operator fixes the secret; the pool reconnects
        with pool.connection():
            pass
        assert pool.connect_outcomes.unresolved_failure() is None
    finally:
        pool.close()
