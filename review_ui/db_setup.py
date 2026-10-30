"""Set up the review gate's durable store (RC1-576, ADR-0022).

    uv run --group ui python -m review_ui.db_setup migrate
    uv run --group ui python -m review_ui.db_setup create-app-role
    uv run --group ui python -m review_ui.db_setup verify
    fly secrets import -a esp-migration-demo < .env.neon

Two credential files, both gitignored and both kept apart from .env so a
local `python -m review_ui` never writes to the live database by accident:

- .env.neon.owner  ESP_DATABASE_OWNER_URL - the database owner; migrations
                   and role management only, never deployed.
- .env.neon        ESP_DATABASE_URL (the least-privilege app role) and
                   ESP_SESSION_TOKEN_KEY - exactly what the app gets, and
                   exactly what `fly secrets import` reads.

Secrets are generated here and written to those files; nothing secret is
printed.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from cryptography.fernet import Fernet
from psycopg import errors, sql

from review_ui.audit import verify_records
from review_ui.pgstore import COLUMNS, row_to_record

SCHEMA = Path(__file__).with_name("schema.sql")
OWNER_ENV = Path(".env.neon.owner")
APP_ENV = Path(".env.neon")
APP_ROLE = "esp_app"


class SetupError(RuntimeError):
    pass


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def write_env_values(path: Path, updates: dict[str, str]) -> None:
    """Set keys in an env file, keeping every other line; owner-only perms."""
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        key = line.partition("=")[0].strip()
        if key in updates:
            lines[i] = f"{key}={updates[key]}"
            seen.add(key)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def owner_url() -> str:
    url = os.environ.get("ESP_DATABASE_OWNER_URL") or read_env_file(OWNER_ENV).get(
        "ESP_DATABASE_OWNER_URL"
    )
    if not url:
        raise SetupError(f"no ESP_DATABASE_OWNER_URL in the environment or {OWNER_ENV}")
    return url


def app_url() -> str:
    url = os.environ.get("ESP_DATABASE_URL") or read_env_file(APP_ENV).get("ESP_DATABASE_URL")
    if not url:
        raise SetupError(f"no ESP_DATABASE_URL in the environment or {APP_ENV}")
    return url


def with_credentials(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}"
    return urlunsplit(parts._replace(netloc=netloc))


def migrate(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(SCHEMA.read_text())


def grant_app_role(conn: psycopg.Connection, role: str) -> None:
    """Exactly the app's privileges, re-applied from scratch each time."""
    ident = sql.Identifier(role)
    dbname = sql.Identifier(conn.info.dbname)
    for statement in (
        sql.SQL("REVOKE ALL ON review_audit, review_sso_sessions FROM {}"),
        sql.SQL("GRANT CONNECT ON DATABASE {db} TO {}"),
        sql.SQL("GRANT USAGE ON SCHEMA public TO {}"),
        sql.SQL("GRANT SELECT, INSERT ON review_audit TO {}"),
        sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON review_sso_sessions TO {}"),
    ):
        conn.execute(statement.format(ident, db=dbname))


def create_app_role(url: str, role: str = APP_ROLE) -> str:
    """Create (or re-password) the app role with a fresh generated password;
    returns the app's connection URL. Created over SQL on purpose: Neon adds
    roles created through its API to neon_superuser, which can write every
    table - the opposite of least privilege."""
    password = secrets.token_urlsafe(32)
    with psycopg.connect(url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(
            sql.SQL(verb + " ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        grant_app_role(conn, role)
        privileged = conn.execute(
            "SELECT array_agg(g.rolname) FROM pg_auth_members m"
            " JOIN pg_roles g ON g.oid = m.roleid JOIN pg_roles r ON r.oid = m.member"
            " WHERE r.rolname = %s",
            (role,),
        ).fetchone()[0]
    if privileged:
        raise SetupError(f"{role} is a member of {privileged}; it must hold no role memberships")
    return with_credentials(url, role, password)


def verify(url: str) -> list[str]:
    """As the app role: the chain is intact, and the database refuses to
    rewrite it. Returns problems; empty means healthy."""
    problems = []
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            f"SELECT {', '.join(COLUMNS)} FROM review_audit ORDER BY seq"
        ).fetchall()
        problems += verify_records((row_to_record(r) for r in rows), "seq")
        state = "intact" if not problems else "BROKEN"
        print(f"review_audit: {len(rows)} record(s), chain {state}")
        for attempt in (
            "UPDATE review_audit SET event = event",
            "DELETE FROM review_audit",
            "TRUNCATE review_audit",
        ):
            try:
                with conn.transaction():
                    conn.execute(attempt)
                problems.append(f"the app role was allowed to run: {attempt}")
            except (errors.InsufficientPrivilege, errors.RaiseException):
                print(f"refused, as it must be: {attempt.split()[0]}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply review_ui/schema.sql as the owner (idempotent)")
    sub.add_parser("create-app-role", help=f"(re)create {APP_ROLE}, write {APP_ENV}")
    sub.add_parser("verify", help="as the app role: chain intact, rewrites refused")
    args = parser.parse_args()
    try:
        if args.command == "migrate":
            migrate(owner_url())
            print("schema applied")
        elif args.command == "create-app-role":
            url = create_app_role(owner_url())
            updates = {"ESP_DATABASE_URL": url}
            if not read_env_file(APP_ENV).get("ESP_SESSION_TOKEN_KEY"):
                updates["ESP_SESSION_TOKEN_KEY"] = Fernet.generate_key().decode()
            write_env_values(APP_ENV, updates)
            print(f"{APP_ROLE} ready; wrote {', '.join(updates)} to {APP_ENV} (mode 600)")
        elif args.command == "verify":
            problems = verify(app_url())
            for problem in problems:
                print(f"PROBLEM: {problem}")
            return 1 if problems else 0
    except (SetupError, psycopg.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
