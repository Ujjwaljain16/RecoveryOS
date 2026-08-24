"""
Phase 3 Throughput Benchmark — POST /v1/events

Target (TRD §8): 500 events/sec sustained, p95 latency under acceptable bounds.

HOW TO RUN:
  1. Start the API server in a separate terminal:
       uvicorn apps.api.main:app --host 0.0.0.0 --port 8000 --workers 4
  2. Ensure Redis and Postgres are running (docker-compose up -d redis postgres)
  3. Run this script:
       python -m tests.performance.test_ingest_throughput

REPORT FORMAT:
  - Requests attempted
  - HTTP 202 accepted
  - HTTP 429 rate limited
  - HTTP 4xx/5xx errors
  - Redis stream message count
  - p50 / p90 / p95 / p99 latency (ms)
  - Actual throughput (req/sec)
  - Silent loss check (must be 0)

NOTE ON RATE LIMITER:
  The test merchant is given a large bucket (capacity=10000, refill=1000/sec)
  so the rate limiter does NOT interfere with the throughput measurement.
  Separate rate-limiter boundary tests are in test_ingest.py.

NOTE ON AUTH (Task 4): this script seeds its own test merchant + a fixed
  API key directly into Postgres on each run (see _seed_merchant_and_api_key)
  — no manual provisioning step needed, just a reachable DATABASE_URL_SYNC.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import redis.asyncio as aioredis

# ─── Config ────────────────────────────────────────────────────────────────────
API_BASE_URL = "http://localhost:8000"
REDIS_URL = "redis://localhost:6379/0"


def _database_url_sync() -> str:
    """
    Matches docker-compose.yml's host port mapping ("5433:5432") — this
    script targets a docker-compose deployment per its own HOW TO RUN
    instructions above, not the config.py default (host-network Postgres on
    5432, a non-Dockerized local dev setup).

    Task 6: the password is read from RECOVERYOS_APP_ROLE_PASSWORD (the same
    var the migration itself reads — see .env), not hardcoded — this script
    used to embed the literal 'recoveryos' password directly, one more copy
    of the same now-rotated, permanently-compromised credential.

    Deliberately NOT called at module import time: this file matches
    pytest's `test_*.py` collection pattern (pyproject.toml), so importing
    it happens on every `pytest tests/` run, including ones that have no
    reason to set this env var — computing the URL lazily, only when the
    benchmark actually runs, keeps collection from failing everywhere else.
    """
    password = os.environ.get("RECOVERYOS_APP_ROLE_PASSWORD")
    if not password:
        raise SystemExit(
            "RECOVERYOS_APP_ROLE_PASSWORD is not set. Export it (matching your "
            ".env) before running this benchmark — see .env.example."
        )
    return f"postgresql://recoveryos:{password}@localhost:5433/recoveryos"


STREAM_NAME = "stream:payment_failed"

TARGET_RPS = 500
DURATION_SECONDS = 10
TOTAL_TARGET = TARGET_RPS * DURATION_SECONDS  # 5,000

# Test merchant with a large bucket so rate limiter doesn't interfere.
# Must be a real UUID: payments.merchant_id is a UUID FK column
# (recoveryos/models.py) — EventPayload.merchant_id has no format
# validation (plain str, apps/api/routers/events.py), so a non-UUID string
# here would 202 at the API and XADD into Redis fine, then fail every
# single consumer-side Postgres write with an "invalid input syntax for
# type uuid" DataError — silently passing this benchmark's own checks
# (which only look at API responses + Redis stream growth) while leaving
# nothing durably persisted downstream.
TEST_MERCHANT_ID = "d9b7a6c1-0000-4000-8000-000000000001"
TEST_BUCKET_CAPACITY = 10_000

# Fixed (not randomly generated per run) so re-running this script doesn't
# require re-discovering a key — the hash is what's actually persisted, and
# re-seeding with the same raw key is idempotent (ON CONFLICT DO UPDATE).
TEST_API_KEY = "rk_live_throughput_benchmark_fixed_test_key_do_not_use_in_prod"


# ─── Metrics ───────────────────────────────────────────────────────────────────
@dataclass
class BenchmarkResults:
    attempted: int = 0
    accepted: int = 0
    rate_limited: int = 0
    errors_4xx: int = 0
    errors_5xx: int = 0
    network_errors: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    elapsed_sec: float = 0.0
    stream_count_before: int = 0
    stream_count_after: int = 0

    @property
    def actual_rps(self) -> float:
        return self.accepted / self.elapsed_sec if self.elapsed_sec > 0 else 0

    @property
    def stream_delta(self) -> int:
        return self.stream_count_after - self.stream_count_before

    @property
    def silent_loss(self) -> int:
        """
        Silent loss = accepted responses with no corresponding stream entry.
        Any accepted request MUST have a corresponding XADD. If not, data was lost.
        """
        return max(0, self.accepted - self.stream_delta)

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        sorted_lat = sorted(self.latencies_ms)
        idx = int(len(sorted_lat) * p / 100)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]

    def print_report(self):
        print("\n" + "=" * 60)
        print("PHASE 3 THROUGHPUT BENCHMARK RESULTS")
        print("=" * 60)
        print(
            f"Target:          {TARGET_RPS} req/s × {DURATION_SECONDS}s = {TOTAL_TARGET:,} requests"
        )
        print()
        print("── Request Outcomes ──────────────────────────────────────")
        print(f"  Attempted:     {self.attempted:,}")
        print(f"  202 Accepted:  {self.accepted:,}")
        print(f"  429 Rate Lim:  {self.rate_limited:,}")
        print(f"  4xx Errors:    {self.errors_4xx:,}")
        print(f"  5xx Errors:    {self.errors_5xx:,}")
        print(f"  Network Err:   {self.network_errors:,}")
        print()
        print("── Latency (ms) ─────────────────────────────────────────")
        if self.latencies_ms:
            print(f"  p50:  {self.percentile(50):.1f}")
            print(f"  p90:  {self.percentile(90):.1f}")
            print(f"  p95:  {self.percentile(95):.1f}")
            print(f"  p99:  {self.percentile(99):.1f}")
            print(f"  min:  {min(self.latencies_ms):.1f}")
            print(f"  max:  {max(self.latencies_ms):.1f}")
        print()
        print("── Throughput ───────────────────────────────────────────")
        print(f"  Elapsed:       {self.elapsed_sec:.2f}s")
        print(f"  Actual RPS:    {self.actual_rps:.1f} req/s (accepted/elapsed)")
        print()
        print("── Redis Stream Integrity ────────────────────────────────")
        print(f"  Stream before: {self.stream_count_before:,}")
        print(f"  Stream after:  {self.stream_count_after:,}")
        print(f"  Stream delta:  {self.stream_delta:,}")
        print(
            f"  Silent loss:   {self.silent_loss:,} {'✓ ZERO' if self.silent_loss == 0 else '✗ DATA LOSS DETECTED'}"
        )
        print()
        if self.actual_rps >= TARGET_RPS:
            print(f"  ✓ TARGET MET: {self.actual_rps:.0f} >= {TARGET_RPS} req/s")
        else:
            print(f"  ⚠ TARGET MISSED: {self.actual_rps:.0f} < {TARGET_RPS} req/s")
            print("    This may be due to local network/CPU constraints.")
            print("    This is reported as a known limitation, not a silent failure.")
        print("=" * 60 + "\n")


def _build_payload() -> dict[str, Any]:
    return {
        "payment_id": str(uuid.uuid4()),
        "merchant_id": TEST_MERCHANT_ID,
        "customer_id": str(uuid.uuid4()),
        "amount_paise": 50000,
        "method": "upi",
        "bank": "HDFC",
        "event_type": "PAYMENT_FAILED",
        "failure_code": "BANK_TIMEOUT",
    }


async def _send_request(
    session: aiohttp.ClientSession,
    results: BenchmarkResults,
) -> None:
    """Send a single event and record the outcome."""
    payload = _build_payload()
    start = time.perf_counter()
    try:
        async with session.post(
            f"{API_BASE_URL}/v1/events",
            json=payload,
            headers={"X-API-Key": TEST_API_KEY},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            elapsed_ms = (time.perf_counter() - start) * 1000
            results.attempted += 1
            results.latencies_ms.append(elapsed_ms)

            if resp.status == 202:
                results.accepted += 1
            elif resp.status == 429:
                results.rate_limited += 1
            elif 400 <= resp.status < 500:
                results.errors_4xx += 1
            elif resp.status >= 500:
                results.errors_5xx += 1
    except (TimeoutError, aiohttp.ClientError):
        results.attempted += 1
        results.network_errors += 1


def _seed_merchant_and_api_key() -> None:
    """
    Task 4 added real API-key auth after this script was originally written
    against the old (unverified X-Merchant-ID) model — every request would
    401 without this. Seeds the test merchant + a fixed, known API key via a
    direct sync Postgres connection (this script has no async DB session of
    its own, and doesn't need one for a one-off idempotent upsert).
    """
    import psycopg2

    from apps.api.dependencies.auth import hash_api_key

    key_hash = hash_api_key(TEST_API_KEY)
    conn = psycopg2.connect(_database_url_sync())
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO merchants (merchant_id, name, api_key_hash) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (merchant_id) DO UPDATE SET api_key_hash = EXCLUDED.api_key_hash",
                (TEST_MERCHANT_ID, "throughput-benchmark-merchant", key_hash),
            )
    finally:
        conn.close()


async def _seed_merchant_bucket(redis: aioredis.Redis) -> None:
    """Pre-seed the test merchant's bucket so it won't be rate-limited during the test."""
    bucket_key = f"rate_limit:events:{TEST_MERCHANT_ID}"
    now_ms = int(
        time.time() * 1000
    )  # matches the real limiter's clock (Task 5) — see rate_limit.py
    await redis.hset(
        bucket_key,
        mapping={
            "tokens": str(TEST_BUCKET_CAPACITY),
            "last_ms": str(now_ms),
        },
    )
    await redis.expire(bucket_key, 300)


async def run_benchmark() -> BenchmarkResults:
    results = BenchmarkResults()
    redis = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)

    _seed_merchant_and_api_key()

    # Seed generous rate limit bucket for test merchant
    await _seed_merchant_bucket(redis)

    # Measure stream size before
    try:
        results.stream_count_before = await redis.xlen(STREAM_NAME)
    except Exception:
        results.stream_count_before = 0

    # Fire requests at TARGET_RPS for DURATION_SECONDS
    connector = aiohttp.TCPConnector(limit=200, limit_per_host=200)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = []
        start_time = time.perf_counter()
        interval = 1.0 / TARGET_RPS  # seconds between requests

        for i in range(TOTAL_TARGET):
            task = asyncio.create_task(_send_request(session, results))
            tasks.append(task)

            # Rate-pace the submissions
            expected_time = start_time + i * interval
            now = time.perf_counter()
            sleep_time = expected_time - now
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        # Wait for all in-flight requests
        await asyncio.gather(*tasks, return_exceptions=True)
        results.elapsed_sec = time.perf_counter() - start_time

    # Allow 1s for final XADDs to land in Redis
    await asyncio.sleep(1)
    results.stream_count_after = await redis.xlen(STREAM_NAME)

    await redis.aclose()
    return results


def main():
    print(f"\nStarting throughput benchmark: {TARGET_RPS} req/s × {DURATION_SECONDS}s...")
    print(f"API: {API_BASE_URL}")
    print(f"Redis: {REDIS_URL}\n")

    results = asyncio.run(run_benchmark())
    results.print_report()

    if results.silent_loss > 0:
        raise SystemExit(f"FATAL: {results.silent_loss} events silently lost!")


if __name__ == "__main__":
    main()
