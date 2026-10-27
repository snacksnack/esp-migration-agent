"""Bounded retries and ambiguous-outcome recovery for target adapters (RC1-570).

Every HTTP failure tells us something different about the target's state, and
the taxonomy here is the whole decision (ADR-0015):

- SAFE: the request was provably not processed - the server said "not now"
  (429 rate limit, 503 unavailable) or the connection never opened
  (connect-phase errors). Replaying is correct for any method: bounded
  retries with backoff, Retry-After honored (capped).
- AMBIGUOUS: the request MAY have been processed - the connection died after
  the request left (read timeouts, dropped sockets) or a gateway answered for
  an origin it lost track of (502, 504). Replaying a non-idempotent write
  here is how duplicates are made. Idempotent calls (GET, PUT, declared
  upserts) replay; every other write must verify the outcome with a
  read-back (`recover`) before re-sending.
- FATAL: the server understood and refused (400, 401 expired credentials,
  403, 404, ...). Retrying cannot help; the caller raises loudly and the
  recovery from a partial run stays what it always was: the idempotent
  re-run, which converges to zero writes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from executor.target import TargetError

MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (0.5, 1.0, 2.0)  # between attempts; last value repeats
RETRY_AFTER_CAP = 30.0  # never trust a header into a multi-minute stall

SAFE_STATUSES = frozenset({429, 503})
AMBIGUOUS_STATUSES = frozenset({502, 504})
# Connect-phase failures: the request never left this process.
SAFE_EXCEPTIONS = (httpx.ConnectError, httpx.ConnectTimeout)

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE"})


@dataclass
class Recovered:
    """A write whose response was lost but whose outcome a read-back confirmed:
    the data stands in for the response body the lost reply would have carried."""

    data: dict


def _retry_after(resp: httpx.Response, fallback: float) -> float:
    try:
        seconds = float(resp.headers.get("Retry-After"))
    except (TypeError, ValueError):  # absent, or an HTTP-date we don't parse
        return fallback
    return max(0.0, min(seconds, RETRY_AFTER_CAP))


def call_with_retries(
    send: Callable[[], httpx.Response],
    *,
    what: str,
    replay_safe: bool,
    recover: Callable[[], dict | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response | Recovered:
    """Run `send` under the failure taxonomy above.

    Returns the response (2xx and FATAL statuses alike - the adapter owns
    status semantics) or `Recovered` when a read-back confirmed an ambiguous
    write landed. `recover` returns the landed object, or None to say the
    write did not land and the send is safe to repeat.
    """
    last = ""
    for attempt in range(MAX_ATTEMPTS):
        delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
        ambiguous = False
        try:
            resp = send()
        except SAFE_EXCEPTIONS as exc:
            last = f"connection error: {exc}"
        except httpx.HTTPError as exc:
            last = f"connection error: {exc}"
            ambiguous = True
        else:
            if resp.status_code in SAFE_STATUSES:
                last = f"HTTP {resp.status_code}"
                delay = _retry_after(resp, delay)
            elif resp.status_code in AMBIGUOUS_STATUSES:
                last = f"HTTP {resp.status_code}"
                ambiguous = True
            else:
                return resp
        if ambiguous and not replay_safe:
            if recover is None:
                raise TargetError(
                    f"{what}: {last}; the write may have landed and no read-back "
                    "is available, so it is not replayed - re-run to converge"
                )
            landed = recover()
            if landed is not None:
                return Recovered(landed)
        if attempt + 1 < MAX_ATTEMPTS:
            sleep(delay)
    raise TargetError(f"{what}: {last} after {MAX_ATTEMPTS} attempts")
