"""Run the review-gate UI backend (RC1-560).

    uv run python -m review_ui [--spec specs/drafts/mailchimp_to_listmonk.yaml]

Dev is two processes, reid_basic style: this backend on :8542 and the Vite
dev server (`npm run dev` in review_ui/frontend) proxying /api here. With a
built frontend (`npm run build`), this process alone serves the whole app.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from ingest.mailchimp import DEFAULT_ACCOUNT_TZ
from review_ui.api import create_app

FIXTURE_AUDIENCE = Path("fixtures/mailchimp_export/audience")
FIXTURE_SUPPLEMENT = Path("fixtures/api_supplement")
DEFAULT_SPEC = Path("specs/drafts/mailchimp_to_listmonk.yaml")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--audience-dir", type=Path, default=FIXTURE_AUDIENCE)
    parser.add_argument("--supplement-dir", type=Path, default=FIXTURE_SUPPLEMENT)
    parser.add_argument("--account-tz", default=DEFAULT_ACCOUNT_TZ)
    parser.add_argument("--target", default="listmonk")
    parser.add_argument(
        "--as-of",
        default="2026-10-24",
        help="anchor date for tenure cohorts (YYYY-MM-DD, UTC midnight); "
        "matches the discovery.report default so the charts agree",
    )
    parser.add_argument("--port", type=int, default=8542)
    parser.add_argument("--host", default="127.0.0.1")  # 0.0.0.0 in the container
    args = parser.parse_args()

    app = create_app(
        args.spec,
        audience_dir=args.audience_dir,
        supplement_dir=args.supplement_dir,
        account_tz=args.account_tz,
        target=args.target,
        as_of=datetime.strptime(args.as_of, "%Y-%m-%d").replace(tzinfo=UTC),
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
