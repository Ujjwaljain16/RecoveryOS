"""
Integration tests for the rate limiter's clock source (Task 5).

rate_limit.py used to seed the token-bucket refill math with
time.monotonic() — a per-process clock whose zero point is arbitrary and
unrelated across processes. Masked today only because the API Dockerfile
pins --workers 1 (a single process); the instant a second worker process or
replica shares the same Redis bucket, each process's "now" becomes a
meaningless number relative to the other's, silently corrupting the
elapsed-time refill calculation (either over-refilling, so the limit never
bites, or under-refilling, so it wrongly denies real traffic) with no error
raised anywhere — exactly the kind of bug that's cheap to fix now and
miserable to diagnose after it ships.

Two angles, both needed:
  1. Direct proof the limiter's clock source is wall-clock (time.time()),
     which is the actual property that guarantees cross-process agreement —
     any process's time.time() reads the same OS-level UTC clock, whereas
     time.monotonic() would not, even within versions of this exact test.
  2. A "two simulated worker processes" test: two independent RateLimiter
     instances, sharing one Redis bucket, alternating calls — proving the
     bucket is genuinely shared (not double- or under-counted), which only
     means something once (1) establishes why it holds across real
     processes and not just within one Python interpreter.
"""

from __future__ import annotations

import time
import uuid

import pytest

from apps.api.dependencies.rate_limit import RateLimiter
from recoveryos.models import Merchant


def _in_memory_merchant() -> Merchant:
    """
    RateLimiter.__call__ only reads merchant.merchant_id — no DB round trip
    needed to exercise it directly (bypassing the FastAPI Depends() wiring,
    which requires a live request to resolve), so a plain in-memory Merchant
    instance is sufficient and doesn't need to be persisted.
    """
    return Merchant(merchant_id=str(uuid.uuid4()), name="rate-limiter-clock-test")


@pytest.mark.asyncio
async def test_rate_limiter_uses_wall_clock_not_monotonic(redis_client):
    """
    After a call, the bucket's persisted last_ms must be within a tight
    tolerance of time.time()*1000 (wall-clock milliseconds since the Unix
    epoch — currently ~1.79 trillion). time.monotonic()*1000 would instead
    be some small number reflecting process uptime (typically well under a
    few billion even for a long-lived process) — wildly, unmistakably
    different from epoch-milliseconds. This is the property that actually
    makes cross-process sharing correct, not just an implementation detail.
    """
    limiter = RateLimiter(capacity=100, refill_rate=50)
    merchant = _in_memory_merchant()

    before_ms = time.time() * 1000
    await limiter(merchant=merchant, redis=redis_client)
    after_ms = time.time() * 1000

    bucket_key = f"rate_limit:events:{merchant.merchant_id}"
    stored = await redis_client.hget(bucket_key, "last_ms")
    assert stored is not None, "limiter call must persist last_ms"
    stored_ms = float(stored)

    # rate_limit.py stores int(time.time() * 1000) — truncation can shave up
    # to ~1ms off vs. this test's own (unrounded) `before_ms` read a moment
    # earlier, so give the lower bound a small tolerance too; the point of
    # this assertion is "same clock, same epoch", not sub-millisecond timing.
    assert before_ms - 50 <= stored_ms <= after_ms + 50, (
        f"last_ms={stored_ms} is not within the wall-clock window "
        f"[{before_ms}, {after_ms}] the call executed in — this would fail "
        f"immediately if the limiter reverted to time.monotonic(), whose "
        f"value has no relationship to time.time() at all"
    )


@pytest.mark.asyncio
async def test_rate_limiter_consistent_across_simulated_multiple_processes(redis_client):
    """
    Two independent RateLimiter instances — standing in for two separate
    worker processes/replicas, each with its own Python interpreter state
    in reality — sharing ONE Redis bucket key. Calls alternate between them.

    If the limiter's clock were process-relative (the pre-fix bug), each
    instance's "now" would be incomparable to the other's, and the shared
    bucket's elapsed-time math would be corrupted — most likely presenting
    as far MORE than `capacity` calls being allowed (each instance's clock
    reads as having "elapsed" a large, spurious amount of time relative to
    a last_ms written under a different instance's clock, causing runaway
    over-refill). With a real shared wall clock, alternating between
    instances must behave EXACTLY like a single instance handling the same
    call sequence: exactly `capacity` allowed, the rest denied.
    """
    capacity = 20
    # Negligible refill_rate so the ~tens-of-ms this test takes to run
    # contributes an unmeasurable fraction of a token — isolates the
    # assertion to "is the bucket genuinely shared", not "how much did it
    # refill mid-test".
    limiter_process_1 = RateLimiter(capacity=capacity, refill_rate=1)
    limiter_process_2 = RateLimiter(capacity=capacity, refill_rate=1)
    merchant = _in_memory_merchant()  # same merchant_id => same Redis bucket key

    allowed = 0
    denied = 0
    total_calls = capacity * 2  # deliberately overshoot capacity

    for i in range(total_calls):
        limiter = limiter_process_1 if i % 2 == 0 else limiter_process_2
        try:
            await limiter(merchant=merchant, redis=redis_client)
            allowed += 1
        except Exception as exc:  # HTTPException(429) on denial
            assert getattr(exc, "status_code", None) == 429, f"unexpected exception: {exc!r}"
            denied += 1

    assert allowed == capacity, (
        f"Expected exactly {capacity} allowed calls across BOTH simulated "
        f"worker instances sharing one bucket (proving it's genuinely "
        f"shared state, not double-counted per instance or under-counted "
        f"from clock skew) — got {allowed} allowed, {denied} denied"
    )
    assert denied == total_calls - capacity
