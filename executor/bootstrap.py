"""Bootstrap an API user on a fresh dev Listmonk (RC1-546).

Listmonk's API takes API-user tokens, not the admin password, so the
docker-compose dev target needs one created once after install. This logs in
with the admin credentials (compose defaults), creates an API user, and
writes the ESP_LISTMONK_* values to a file; the token is only ever available
at creation.

    uv run python -m executor.bootstrap            # writes .env.listmonk
    set -a; . ./.env.listmonk; set +a              # load it into the shell

CI points it straight at the step environment:

    uv run python -m executor.bootstrap --out "$GITHUB_ENV" --append

Local demo targets only - never point this at anything real.

**The token is never printed** (RC1-617). It used to go to stdout, which both
call sites happened to capture rather than log, so no token ever reached a
log - but "the caller redirects it" is not a property this module can
guarantee, and one `tee` or one dropped redirect in a public repo's workflow
would put a credential in a build log. CodeQL flagged it as
`py/clear-text-logging-sensitive-data` and was right about the pattern. A
credential-provisioning tool cannot avoid emitting a credential, so it emits
to a file the caller names, at mode 0600, and prints only where it put it.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

import httpx

API_USERNAME = "esp-executor"
DEFAULT_OUT = ".env.listmonk"
# Owner read/write. Applied when this process creates the file; an existing
# file the caller named (notably $GITHUB_ENV) keeps its own permissions,
# because tightening a file we do not own is the kind of surprise a bootstrap
# script has no business springing on its caller.
SECRET_MODE = 0o600


class BootstrapError(RuntimeError):
    pass


def create_api_user(url: str, admin_user: str, admin_password: str) -> tuple[str, str]:
    """Returns (username, token) for a fresh API user; errors loudly if one
    of this name already exists (its token is unrecoverable - delete it in
    the UI or recreate the container)."""
    base = url.rstrip("/")
    with httpx.Client(timeout=30, follow_redirects=False) as http:
        resp = http.get(f"{base}/admin/login")
        nonce = resp.cookies.get("nonce")
        if not nonce:
            raise BootstrapError(f"no login nonce from {base}/admin/login")
        resp = http.post(
            f"{base}/admin/login",
            data={
                "username": admin_user,
                "password": admin_password,
                "nonce": nonce,
                "next": "/admin",
            },
            cookies=resp.cookies,
        )
        if resp.status_code != 302:
            raise BootstrapError(f"admin login failed: HTTP {resp.status_code}")
        resp = http.post(
            f"{base}/api/users",
            json={
                "username": API_USERNAME,
                "name": "ESP executor",
                "type": "api",
                "status": "enabled",
                "user_role_id": 1,
            },
        )
        if resp.status_code >= 400:
            raise BootstrapError(
                f"create API user failed: HTTP {resp.status_code} {resp.text[:300]}"
            )
        token = resp.json()["data"]["password"]
        return API_USERNAME, token


def env_lines(url: str, username: str, token: str) -> list[str]:
    """The three settings the Listmonk adapter reads, as KEY=value lines."""
    return [
        f"ESP_LISTMONK_URL={url}",
        f"ESP_LISTMONK_USERNAME={username}",
        f"ESP_LISTMONK_PASSWORD={token}",
    ]


def write_env(path: pathlib.Path, lines: list[str], *, append: bool) -> None:
    """Write the credentials to `path`, 0600 when we are the one creating it.

    Append mode exists for `$GITHUB_ENV`, which already exists and belongs to
    the runner; overwriting it would discard whatever earlier steps put there.
    """
    existed = path.exists()
    with path.open("a" if append else "w") as handle:
        handle.write("\n".join(lines) + "\n")
    if not (append and existed):
        os.chmod(path, SECRET_MODE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:9000")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-password", default="listmonk-admin")
    parser.add_argument(
        "--out", default=DEFAULT_OUT,
        help=f"file to write the ESP_LISTMONK_* lines to (default {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--append", action="store_true",
        help="append instead of overwriting; for $GITHUB_ENV",
    )
    args = parser.parse_args(argv)
    try:
        username, token = create_api_user(args.url, args.admin_user, args.admin_password)
    except (BootstrapError, httpx.HTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    out = pathlib.Path(args.out)
    try:
        write_env(out, env_lines(args.url, username, token), append=args.append)
    except OSError as exc:
        # The token is unrecoverable once created, so say so rather than
        # letting the caller think a retry will work.
        print(f"error: created the API user but could not write {out}: {exc}",
              file=sys.stderr)
        print("the token is only shown at creation; delete the API user in "
              "the Listmonk UI (or recreate the container) and re-run",
              file=sys.stderr)
        return 1
    print(f"ESP_LISTMONK_URL={args.url}")
    print(f"ESP_LISTMONK_USERNAME={username}")
    print(f"wrote the API token to {out} (not shown here)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
