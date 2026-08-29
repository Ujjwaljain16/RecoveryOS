"""
Integration tests for the Event Ingest path (Phase 3).

Tests:
  1. test_malformed_event_rejected_with_422       — Pydantic strict validation
  2. test_rate_limit_enforced_per_merchant        — Redis token bucket 429
  3. test_post_event_writes_to_db_and_publishes   — E2E: API → Redis → consumer → DB
  4. test_duplicate_event_is_idempotent           — Same event_id does not create 2 DB rows
  5. test_merchant_identity_mismatch_rejected     — Anti-spoofing: body vs API-key-verified merchant_id
  6. test_consumer_restart_recovers_pending       — Kill consumer mid-batch, restart, all processed
  7. test_db_unavailable_leaves_message_pending   — DB outage leaves message in PEL

Requirements:
  - Real Redis (testcontainers or local)
  - Real Postgres (testcontainers via conftest.py)
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select

from apps.api.dependencies.auth import generate_api_key
from tests.integration.conftest import seed_merchant_and_customer as _seed_merchant_and_customer
from tests.integration.conftest import seed_merchant_with_api_key as _seed_merchant_with_api_key
from tests.integration.conftest import to_async_url as _to_async_url

# ─── Test infrastructure ───────────────────────────────────────────────────────
# Shared fixtures (redis_container, redis_url, patch_settings, redis_client,
# app, async_client) and helpers (to_async_url, seed_merchant_and_customer)
# now live in tests/integration/conftest.py (Task 4) — test_auth.py needs the
# same plumbing and duplicating it across files is how they silently drift.

STREAM_NAME = "stream:payment_failed"
STREAM_RISK = "stream:risk_engine"


def _valid_event(**overrides) -> dict:
    """Build a valid event payload."""
    mid = overrides.pop("merchant_id", str(uuid.uuid4()))
    base = {
        "payment_id": str(uuid.uuid4()),
        "merchant_id": mid,
        "customer_id": str(uuid.uuid4()),
        "amount_paise": 50000,
        "method": "upi",
        "bank": "HDFC",
        "event_type": "PAYMENT_FAILED",
        "failure_code": "BANK_TIMEOUT",
    }
    base.update(overrides)
    return base, mid


async def _seeded_merchant(migrated_db: str) -> tuple[str, str]:
    """
    Seed a fresh merchant with a real, verifiable API key (Task 4).
    Returns (merchant_id, raw_api_key) — the raw key is what a real caller
    would present via X-API-Key; only its hash is ever persisted.
    """
    merchant_id = str(uuid.uuid4())
    raw_key = generate_api_key()
    await _seed_merchant_with_api_key(
        migrated_db, merchant_id, f"test-merchant-{merchant_id[:8]}", raw_key
    )
    return merchant_id, raw_key


# ─── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_malformed_event_rejected_with_422(async_client, migrated_db):
    """
    Invalid payload (missing required fields, wrong method enum) must return 422.
    Uses a real, valid API key so the 422 is unambiguously about the body,
    not conflated with the auth check this task adds in front of it.
    """
    merchant_id, api_key = await _seeded_merchant(migrated_db)
    bad_payloads = [
        # Missing amount_paise
        {
            "payment_id": str(uuid.uuid4()),
            "merchant_id": merchant_id,
            "customer_id": str(uuid.uuid4()),
            "method": "upi",
            "event_type": "PAYMENT_FAILED",
        },
        # amount_paise = 0 (must be > 0)
        {
            "payment_id": str(uuid.uuid4()),
            "merchant_id": merchant_id,
            "customer_id": str(uuid.uuid4()),
            "amount_paise": 0,
            "method": "upi",
            "event_type": "PAYMENT_FAILED",
        },
        # method not in enum
        {
            "payment_id": str(uuid.uuid4()),
            "merchant_id": merchant_id,
            "customer_id": str(uuid.uuid4()),
            "amount_paise": 10000,
            "method": "cash",
            "event_type": "PAYMENT_FAILED",
        },
        # Extra unknown field (extra="forbid")
        {
            "payment_id": str(uuid.uuid4()),
            "merchant_id": merchant_id,
            "customer_id": str(uuid.uuid4()),
            "amount_paise": 10000,
            "method": "upi",
            "event_type": "PAYMENT_FAILED",
            "unknown_field": "evil",
        },
    ]

    for bad in bad_payloads:
        resp = await async_client.post(
            "/v1/events",
            json=bad,
            headers={"X-API-Key": api_key},
        )
        assert (
            resp.status_code == 422
        ), f"Expected 422 for payload {bad}, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_rate_limit_enforced_per_merchant(async_client, redis_client, migrated_db):
    """
    Hammer a single merchant with requests exceeding bucket capacity
    (production capacity=1000, refill=500/s — apps/api/routers/events.py).
    Assert that some requests receive 429.
    Assert that Merchant B is NOT affected by Merchant A's exhausted bucket.
    """
    import time

    merchant_a, key_a = await _seeded_merchant(migrated_db)
    merchant_b, key_b = await _seeded_merchant(migrated_db)

    # Pre-exhaust merchant_a's bucket. last_ms MUST be seeded from the same
    # clock the Lua script reads (time.time(), wall-clock UTC epoch — Task 5
    # switched this away from time.monotonic(), a per-process, arbitrary-
    # epoch clock that would silently corrupt the shared bucket the moment
    # more than one worker process shares this Redis instance — see
    # apps/api/dependencies/rate_limit.py). NOT 0: that would read as
    # 1970-01-01, making the very next check see a huge elapsed time and
    # fully refill the bucket before the assertion runs, silently turning
    # this into a no-op test.
    #
    # Even seeding last_ms="now" leaves a real (if small) race: some
    # nonzero wall-clock time elapses between this seed and the Lua script's
    # own time.time() read inside the request (fixture setup, ASGI dispatch,
    # event-loop scheduling) — at refill_rate=500/s that gap only needs to
    # be ~2ms to add back a full token and flake this test green when the
    # limiter would actually have allowed the request through. Seed last_ms
    # slightly in the FUTURE instead: the Lua script clamps elapsed_sec to
    # max(0, ...), so any last_ms >= the script's own now_ms guarantees
    # exactly zero refill, deterministically, regardless of how much real
    # time passes before the check runs.
    bucket_key = f"rate_limit:events:{merchant_a}"
    now_ms = (
        int(time.time() * 1000) + 5000
    )  # 5s into the future — comfortably beyond any realistic scheduling delay
    await redis_client.hset(bucket_key, mapping={"tokens": "0", "last_ms": str(now_ms)})

    payload, _ = _valid_event(merchant_id=merchant_a)

    # Next request for merchant_a should be rate-limited
    resp = await async_client.post(
        "/v1/events",
        json=payload,
        headers={"X-API-Key": key_a},
    )
    assert resp.status_code == 429, f"Expected 429, got {resp.status_code}"
    body = resp.json()
    assert body["detail"]["error"] == "rate_limit_exceeded"
    assert "retry_after_ms" in body["detail"]

    # Merchant B should NOT be affected
    payload_b, _ = _valid_event(merchant_id=merchant_b)
    resp_b = await async_client.post(
        "/v1/events",
        json=payload_b,
        headers={"X-API-Key": key_b},
    )
    assert (
        resp_b.status_code == 202
    ), f"Merchant B should succeed but got {resp_b.status_code}: {resp_b.text}"


@pytest.mark.asyncio
async def test_merchant_identity_mismatch_rejected(async_client, migrated_db):
    """
    Body merchant_id != the merchant resolved from a VALID X-API-Key → 403.

    Task 4: this is now a genuinely meaningful anti-spoofing check — the key
    is real and verified; the body just disagrees with what it says. Before
    this task, both sides of this comparison were equally unverified
    client input, so this check was, in effect, decorative.
    """
    real_merchant, api_key = await _seeded_merchant(migrated_db)
    other_merchant_id = str(uuid.uuid4())  # some merchant NOT owned by this key
    payload, _ = _valid_event(merchant_id=other_merchant_id)

    resp = await async_client.post(
        "/v1/events",
        json=payload,
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "merchant_identity_mismatch"


@pytest.mark.asyncio
async def test_post_event_writes_to_db_and_publishes_to_stream(
    async_client, redis_client, migrated_db
):
    """
    End-to-end: POST event → Redis stream → consumer processes → DB row created.
    Also asserts downstream stream:risk_engine receives the event.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    merchant_id, api_key = await _seeded_merchant(migrated_db)
    payload, _ = _valid_event(merchant_id=merchant_id)
    await _seed_merchant_and_customer(migrated_db, merchant_id, payload["customer_id"])

    # POST to API
    resp = await async_client.post(
        "/v1/events",
        json=payload,
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert "event_id" in body
    assert "stream_id" in body
    event_id = body["event_id"]

    # Verify message landed in Redis stream
    stream_len = await redis_client.xlen(STREAM_NAME)
    assert stream_len >= 1

    # Run consumer to process the message
    from services.event_processor.consumer import run_consumer

    async with asyncio.timeout(5):
        consumer_task = asyncio.create_task(run_consumer(redis_client))
        await asyncio.sleep(2)  # give consumer time to process
        consumer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer_task

    # Verify DB rows created
    db_url_async = _to_async_url(migrated_db)
    engine = create_async_engine(db_url_async)
    async with AsyncSession(engine) as session:
        from recoveryos.models import Event, Payment

        payment_row = await session.get(Payment, payload["payment_id"])
        event_rows = (
            (await session.execute(select(Event).where(Event.payment_id == payload["payment_id"])))
            .scalars()
            .all()
        )

    await engine.dispose()

    assert payment_row is not None, "Payment row missing from DB"
    assert len(event_rows) == 1, f"Expected 1 event row, got {len(event_rows)}"
    assert event_rows[0].event_id == event_id
    # Live E2E smoke test finding (2026-08-29): failure_class used to be
    # left NULL for every real API-ingested payment (EventPayload has no
    # such field), which crashed propensity scoring the moment this payment
    # reached decisioning -- see services/event_processor/repository.py's
    # classify_failure().
    assert payment_row.failure_class is not None, (
        "failure_class must never be NULL for a real ingested payment -- "
        "build_propensity_context() requires it and raises otherwise"
    )

    # Verify downstream stream received the event
    risk_len = await redis_client.xlen(STREAM_RISK)
    assert risk_len >= 1


@pytest.mark.asyncio
async def test_duplicate_event_is_idempotent(async_client, redis_client, migrated_db):
    """
    Posting the same idempotency_key twice must NOT create 2 DB event rows.
    Both POSTs return 202 (the second is a safe no-op).
    """
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    merchant_id, api_key = await _seeded_merchant(migrated_db)
    idem_key = str(uuid.uuid4())
    payload, _ = _valid_event(merchant_id=merchant_id)
    payload["idempotency_key"] = idem_key
    await _seed_merchant_and_customer(migrated_db, merchant_id, payload["customer_id"])

    # Post twice
    for _ in range(2):
        resp = await async_client.post(
            "/v1/events",
            json=payload,
            headers={"X-API-Key": api_key},
        )
        assert resp.status_code == 202

    # Run consumer — will see 2 stream entries with same payment_id+event_type
    from services.event_processor.consumer import run_consumer

    async with asyncio.timeout(5):
        task = asyncio.create_task(run_consumer(redis_client))
        await asyncio.sleep(2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    db_url_async = _to_async_url(migrated_db)
    engine = create_async_engine(db_url_async)
    async with AsyncSession(engine) as session:
        from recoveryos.models import Event

        event_rows = (
            (await session.execute(select(Event).where(Event.payment_id == payload["payment_id"])))
            .scalars()
            .all()
        )

    await engine.dispose()

    # Each POST mints its OWN server-side event_id (events.py:138) even though
    # both share the same client idempotency_key — so this is a genuine test of
    # cross-request idempotency, not same-message redelivery: two distinct
    # stream messages, two distinct event_ids, one shared idempotency_key.
    # insert_event_idempotent() dedupes on (payment_id, idempotency_key) via
    # ON CONFLICT DO NOTHING (repository.py), backed by the UNIQUE constraint
    # on events(payment_id, idempotency_key) added in migration 0005 — so
    # exactly one row should land, not "at most 2".
    assert len(event_rows) == 1, (
        f"Same idempotency_key across 2 POSTs must yield exactly 1 events row, "
        f"got {len(event_rows)}"
    )


@pytest.mark.asyncio
async def test_consumer_restart_recovers_pending(redis_client, migrated_db):
    """
    Simulate consumer crash mid-batch (XACK withheld for some messages).
    After restart, pending messages are reclaimed and processed.
    Verifies all 10 events end up in Postgres exactly once.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from services.event_processor.consumer import STREAM_NAME, _ensure_consumer_group

    await _ensure_consumer_group(redis_client)

    payment_ids = []
    merchant_id = str(uuid.uuid4())

    # Publish 10 events directly to the stream (bypassing API)
    for _ in range(10):
        pid = str(uuid.uuid4())
        eid = str(uuid.uuid4())
        cid = str(uuid.uuid4())
        payment_ids.append((pid, eid))
        await _seed_merchant_and_customer(migrated_db, merchant_id, cid)
        await redis_client.xadd(
            STREAM_NAME,
            {
                "event_id": eid,
                "idempotency_key": eid,
                "payment_id": pid,
                "merchant_id": merchant_id,
                "customer_id": cid,
                "amount_paise": "10000",
                "method": "upi",
                "bank": "HDFC",
                "event_type": "PAYMENT_FAILED",
                "failure_code": "BANK_TIMEOUT",
            },
        )

    # First consumer run — process first 5, then "crash" (cancel)
    from services.event_processor.consumer import run_consumer

    processed_count = [0]

    async def counting_process(msg, session, redis):
        nonlocal processed_count
        from services.event_processor.processor import process_event as _orig

        result = await _orig(msg, session, redis)
        if result:
            processed_count[0] += 1
        if processed_count[0] >= 5:
            raise asyncio.CancelledError("Simulated crash after 5")
        return result

    # NOTE: consumer.py does `from ...processor import process_event`, binding
    # its own name in consumer's namespace — patching "processor.process_event"
    # would leave that separate binding (and therefore _process_batch's call)
    # untouched. The name to patch is where consumer.py looks it up.
    async with asyncio.timeout(10):
        with patch("services.event_processor.consumer.process_event", side_effect=counting_process):
            task = asyncio.create_task(run_consumer(redis_client))
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # _reclaim_pending() only reclaims messages idle >= PENDING_RECLAIM_IDLE_MS
    # (5000ms, consumer.py) — messages 6-10 were fetched by XREADGROUP but never
    # attempted before the simulated crash, and message 5 was processed but its
    # ACK was lost to the raised CancelledError, so all of them are sitting in
    # the PEL as of a few milliseconds ago. Wait past that idle threshold so the
    # second run's startup reclaim actually picks them up, rather than the test
    # passing only because nothing was really stuck.
    await asyncio.sleep(5.5)

    # Second consumer run — should reclaim pending messages
    async with asyncio.timeout(10):
        task = asyncio.create_task(run_consumer(redis_client))
        await asyncio.sleep(3)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # All 10 payments should be in DB
    db_url_async = _to_async_url(migrated_db)
    engine = create_async_engine(db_url_async)
    async with AsyncSession(engine) as session:
        from recoveryos.models import Payment

        all_ids = [pid for pid, _ in payment_ids]
        result = await session.execute(
            select(Payment.payment_id).where(Payment.payment_id.in_(all_ids))
        )
        found = {row[0] for row in result.fetchall()}

    await engine.dispose()

    assert found == set(all_ids), f"Missing payments after consumer restart: {set(all_ids) - found}"


@pytest.mark.asyncio
async def test_db_unavailable_leaves_message_pending(redis_client, migrated_db):
    """
    If Postgres is unreachable while processing a message, the message must
    stay in the consumer group's Pending Entry List — not be silently
    dropped, and not be XACK'd as if it had succeeded. Once Postgres is
    reachable again, the same message (not a duplicate) is what lands in the
    DB, and no other message in the stream is affected.

    services/event_processor/processor.py:process_event catches any
    exception, rolls back, and returns False — the consumer only XACKs on
    True (consumer.py:70-75) — so a DB outage should manifest exactly as a
    withheld ACK, never a swallowed error.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from services.event_processor.consumer import (
        GROUP_NAME,
        STREAM_NAME,
        _ensure_consumer_group,
    )

    await _ensure_consumer_group(redis_client)

    payment_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())

    # Seeded up front (not just after "recovery") since it must be in place
    # by the time the recovery-phase consumer run does the real DB write —
    # the simulated outage only fails upsert_payment itself, it doesn't skip
    # the payments_merchant_id_fkey/payments_customer_id_fkey checks.
    await _seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    await redis_client.xadd(
        STREAM_NAME,
        {
            "event_id": event_id,
            "idempotency_key": event_id,
            "payment_id": payment_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "amount_paise": "10000",
            "method": "upi",
            "bank": "HDFC",
            "event_type": "PAYMENT_FAILED",
            "failure_code": "BANK_TIMEOUT",
        },
    )

    # ── Simulate a DB outage: upsert_payment raises for every call ──────────
    from services.event_processor.consumer import run_consumer

    # NOTE: processor.py does `from ...repository import upsert_payment`, so the
    # name to patch is where it's *used* (processor), not where it's defined
    # (repository) — patching the latter would leave processor's already-bound
    # reference untouched.
    with patch(
        "services.event_processor.processor.upsert_payment",
        side_effect=RuntimeError("simulated Postgres outage"),
    ):
        async with asyncio.timeout(5):
            task = asyncio.create_task(run_consumer(redis_client))
            await asyncio.sleep(2)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # Nothing should have been persisted...
    db_url_async = _to_async_url(migrated_db)
    engine = create_async_engine(db_url_async)
    async with AsyncSession(engine) as session:
        from recoveryos.models import Payment

        row = await session.get(Payment, payment_id)
    assert row is None, "Payment must not be persisted while the DB write is failing"

    # ...and the message must still be pending (un-ACKed) for this consumer group.
    pending = await redis_client.xpending(STREAM_NAME, GROUP_NAME)
    assert pending["pending"] >= 1, (
        "Message must remain in the PEL after a failed processing attempt, "
        "not be silently dropped or falsely ACK'd"
    )

    # _reclaim_pending() only reclaims messages idle >= PENDING_RECLAIM_IDLE_MS
    # (5000ms) — wait past that so the recovery run's startup reclaim actually
    # picks the stuck message back up via XAUTOCLAIM, matching how a real
    # consumer restart would behave (not an artificially-fast redelivery).
    await asyncio.sleep(5.5)

    # ── "Postgres recovers" — rerun the consumer with the outage lifted ─────
    async with asyncio.timeout(5):
        task = asyncio.create_task(run_consumer(redis_client))
        await asyncio.sleep(2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async with AsyncSession(engine) as session:
        from recoveryos.models import Event, Payment

        row = await session.get(Payment, payment_id)
        event_rows = (
            (await session.execute(select(Event).where(Event.payment_id == payment_id)))
            .scalars()
            .all()
        )
    await engine.dispose()

    assert row is not None, "Payment must be persisted once the DB is reachable again"
    assert len(event_rows) == 1, (
        f"Expected exactly 1 event row after recovery (no duplicate from the "
        f"retried delivery), got {len(event_rows)}"
    )
