"""Bootstrap an API user on a fresh dev Listmonk (RC1-546).

Listmonk's API takes API-user tokens, not the admin password, so the
docker-compose dev target needs one created once after install. This logs in
with the admin credentials (compose defaults), creates an API user, and
prints the ESP_LISTMONK_* values; the token is only ever shown at creation.

    uv run python -m executor.bootstrap [--url http://localhost:9000]
    eval "$(uv run python -m executor.bootstrap --export)"

CI appends the output to $GITHUB_ENV. Local demo targets only - never point
this at anything real.
"""

from __future__ import annotations

import argparse
import sys

import httpx

API_USERNAME = "esp-executor"


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:9000")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-password", default="listmonk-admin")
    parser.add_argument(
        "--export",
        action="store_true",
        help="print shell export lines (eval-able) instead of KEY=value",
    )
    args = parser.parse_args()
    try:
        username, token = create_api_user(args.url, args.admin_user, args.admin_password)
    except (BootstrapError, httpx.HTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    prefix = "export " if args.export else ""
    print(f"{prefix}ESP_LISTMONK_URL={args.url}")
    print(f"{prefix}ESP_LISTMONK_USERNAME={username}")
    print(f"{prefix}ESP_LISTMONK_PASSWORD={token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
