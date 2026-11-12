"""Install-kit conformance helpers (RC1-641, ADR-0029).

    python -m conformance fixture --run-id RUN --out DIR [--count N] [--domain D]
    python -m conformance cleanup --expected DIR/expected.json [--out FILE]

`fixture` writes a namespaced synthetic export, a DRAFT spec bound to it, and
expected.json. The caller approves the draft through mapping.review and runs
executor.run / executor.reconcile as for any migration. `cleanup` removes
that run's records from Brevo (namespace-fenced) and reports what remains.

Exit codes: 0 ok (cleanup: nothing remains), 1 cleanup left something,
2 refused or could not run. Output is JSON on stdout (and --out).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from conformance.cleanup import CleanupRefused, cleanup, guard
from conformance.fixture import DEFAULT_COUNT, DEFAULT_DOMAIN, FixtureError, build
from executor.clients import client_for_target
from executor.target import TargetError


def _emit(result: dict, out: Path | None) -> None:
    text = json.dumps(result, indent=1, sort_keys=True) + "\n"
    if out is not None:
        out.write_text(text)
    sys.stdout.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fx = sub.add_parser("fixture")
    fx.add_argument("--run-id", required=True)
    fx.add_argument("--out", type=Path, required=True)
    fx.add_argument("--count", type=int, default=DEFAULT_COUNT)
    fx.add_argument("--domain", default=DEFAULT_DOMAIN)
    cl = sub.add_parser("cleanup")
    cl.add_argument("--expected", type=Path, required=True)
    cl.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        if args.command == "fixture":
            expected = build(args.run_id, args.out, count=args.count, domain=args.domain)
            listed = ("emails", "suppressed_emails")
            summary = {k: v for k, v in expected.items() if k not in listed}
            summary["suppressed"] = len(expected["suppressed_emails"])
            _emit(summary, None)
            return 0
        expected = json.loads(args.expected.read_text())
        guard(expected)  # before any credential is read
        client = client_for_target(expected["target"], need_writes=True)
        result = cleanup(client, expected)
        _emit(result, args.out)
        return 0 if result["clean"] else 1
    except (FixtureError, CleanupRefused, TargetError, OSError, KeyError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
