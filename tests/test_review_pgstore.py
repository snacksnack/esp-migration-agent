"""Durable review-gate state in Postgres (RC1-576, ADR-0022).

Runs against a real Postgres: ESP_TEST_DATABASE_URL must be a superuser URL
to a throwaway database (CI: a postgres:18 service container; locally:
`docker run -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:18`). Skipped
without it - read from the process env only, never .env.

Each test re-creates the schema and connects the app as the least-privilege
role db_setup creates, so every check runs under the production grants.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from review_ui import auth
from review_ui.api import create_app
from review_ui.config import ReviewUiSettings
from review_ui.db_setup import create_app_role, migrate, verify
from review_ui.pgstore import PostgresAuditLog, StoreNotReady, open_pool
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
        conn.execute("DROP TABLE IF EXISTS review_audit, review_sso_sessions")


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


def test_deactivation_still_ends_access_and_deletes_the_grant(tmp_path, db, apps):
    idp = FakeIdp()
    client = apps(tmp_path, idp, db, KEY)
    sso_login(client)
    assert admin_rows("SELECT count(*) FROM review_sso_sessions") == [(1,)]
    idp.active["dana"] = False
    resp = client.patch(
        "/api/mappings/consent/0", json={"source": "subscribed", "status": "approved"}
    )
    assert resp.status_code == 401
    assert admin_rows("SELECT count(*) FROM review_sso_sessions") == [(0,)]
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
