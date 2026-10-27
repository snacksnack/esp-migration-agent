"""RC1-570: the retry policy in isolation - failure classification, backoff
and Retry-After arithmetic, the attempt bound, and ambiguous-outcome recovery.
No network, no adapters: `send` is a scripted callable."""

from __future__ import annotations

import httpx
import pytest

from executor.retries import (
    BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    RETRY_AFTER_CAP,
    Recovered,
    call_with_retries,
)
from executor.target import TargetError


def scripted(*outcomes):
    """A send() that replays `outcomes` (responses returned, exceptions
    raised), counting calls."""
    it = iter(outcomes)
    calls: list = []

    def send() -> httpx.Response:
        item = next(it)
        calls.append(item)
        if isinstance(item, Exception):
            raise item
        return item

    return send, calls


def run(send, *, replay_safe=True, recover=None, sleeps=None):
    return call_with_retries(
        send,
        what="unit call",
        replay_safe=replay_safe,
        recover=recover,
        sleep=(sleeps if sleeps is not None else []).append,
    )


# --- safe failures: provably unprocessed, replayed for any method ---------------


def test_success_passes_through_without_sleeping():
    send, calls = scripted(httpx.Response(200))
    sleeps: list[float] = []
    resp = run(send, sleeps=sleeps)
    assert resp.status_code == 200
    assert len(calls) == 1 and sleeps == []


def test_rate_limit_honors_retry_after():
    send, calls = scripted(
        httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)
    )
    sleeps: list[float] = []
    assert run(send, sleeps=sleeps).status_code == 200
    assert sleeps == [7.0]
    assert len(calls) == 2


def test_retry_after_is_capped_and_garbage_falls_back():
    send, _ = scripted(
        httpx.Response(429, headers={"Retry-After": "9999"}),
        httpx.Response(429, headers={"Retry-After": "Fri, 31 Dec 1999 23:59:59 GMT"}),
        httpx.Response(200),
    )
    sleeps: list[float] = []
    run(send, sleeps=sleeps)
    assert sleeps == [RETRY_AFTER_CAP, BACKOFF_SECONDS[1]]


def test_persistent_rate_limit_exhausts_the_bound():
    send, calls = scripted(*[httpx.Response(429)] * (MAX_ATTEMPTS + 2))
    sleeps: list[float] = []
    with pytest.raises(TargetError, match=f"HTTP 429 after {MAX_ATTEMPTS} attempts"):
        run(send, sleeps=sleeps)
    assert len(calls) == MAX_ATTEMPTS  # bounded: the extra scripted responses unused
    assert sleeps == [0.5, 1.0, 2.0]  # the backoff schedule, no sleep after the last


def test_connect_errors_replay_even_non_idempotent_writes():
    # The connection never opened, so the request was never sent: replaying a
    # POST is safe and no read-back is consulted.
    def recover_must_not_run():
        raise AssertionError("recover() consulted for a provably unsent request")

    send, calls = scripted(httpx.ConnectError("refused"), httpx.Response(201))
    resp = run(send, replay_safe=False, recover=recover_must_not_run)
    assert resp.status_code == 201
    assert len(calls) == 2


# --- ambiguous failures: the write may have landed ------------------------------


def test_timeout_on_idempotent_call_is_replayed():
    send, calls = scripted(httpx.ReadTimeout("read timed out"), httpx.Response(200))
    assert run(send, replay_safe=True).status_code == 200
    assert len(calls) == 2


def test_ambiguous_write_with_landed_readback_is_not_resent():
    send, calls = scripted(httpx.ReadTimeout("read timed out"))
    result = run(send, replay_safe=False, recover=lambda: {"id": 7})
    assert isinstance(result, Recovered)
    assert result.data == {"id": 7}
    assert len(calls) == 1  # the write was never replayed


def test_ambiguous_write_not_landed_is_retried():
    reads: list[int] = []

    def recover() -> dict | None:
        reads.append(1)
        return None  # the write did not land

    send, calls = scripted(httpx.ReadTimeout("read timed out"), httpx.Response(201))
    assert run(send, replay_safe=False, recover=recover).status_code == 201
    assert reads == [1] and len(calls) == 2


def test_ambiguous_write_without_readback_raises_instead_of_replaying():
    send, calls = scripted(httpx.ReadTimeout("read timed out"))
    with pytest.raises(TargetError, match="may have landed"):
        run(send, replay_safe=False)
    assert len(calls) == 1


def test_gateway_statuses_are_ambiguous_for_writes():
    send, calls = scripted(httpx.Response(502))
    result = run(send, replay_safe=False, recover=lambda: {"id": 1})
    assert isinstance(result, Recovered)
    assert len(calls) == 1


# --- fatal statuses: the caller's problem, never retried ------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
def test_fatal_statuses_return_to_the_caller_untouched(status):
    send, calls = scripted(httpx.Response(status))
    sleeps: list[float] = []
    resp = run(send, replay_safe=False, sleeps=sleeps)
    assert resp.status_code == status
    assert len(calls) == 1 and sleeps == []
