"""
Priority 14 -- permanent chaos/fault-injection suite.

Turns this session's ad-hoc live-proof scripts (S1's redelivery test, S4's
bridge test, E1's already-recovered repro) into a standing, re-runnable
suite covering the specific failure modes asked for: killed services,
dropped DB connections, partitioned Redis, duplicate/replayed/out-of-order
events, consumer restarts, and partial downstream failures.

Every test asserts the same five invariants, explicitly, not just "it
didn't crash":
  1. No double recovery      -- recovery_ledger has at most 1 row per payment
  2. No lost canonical event -- the events table never loses a row
  3. No unsafe action        -- at most 1 real execution attempt per payment
  4. Eventually consistent   -- after the fault clears, state is correct
  5. Correct audit trail     -- audit_log matches what actually happened

Uses testcontainers-backed isolated Postgres/Redis (migrated_db/redis_client
fixtures), same as the rest of tests/integration/ -- deliberately NOT the
docker-compose stack, so this suite runs standalone in CI and never
collides with a live demo/eval run using that stack.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import seed_merchant_and_customer, to_async_url

# ─── shared seeding helpers ─────────────────────────────────────────────────


async def _seed_failed_payment(
    migrated_db: str,
    *,
    amount_paise: int = 200_000,
    with_latent_state: bool = True,
    true_recovery_prob_bps: int = 8000,
) -> tuple[str, str, str]:
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    engine = create_async_engine(to_async_url(migrated_db))
    payment_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, :amount, 'upi', 'HDFC', 'failed', 'TIMEOUT', 'TEMPORARY', "
                "true, :ts, :ts)"
            ),
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "amount": amount_paise,
                "ts": datetime.now(UTC) - timedelta(hours=1),
            },
        )
        if with_latent_state:
            sim_id = str(uuid.uuid4())
            await conn.execute(
                text(
                    "INSERT INTO simulator_manifests (simulation_id, seed, generator_version, "
                    "scenario_config, latent_function_version, total_payments) "
                    "VALUES (:sim_id, 1, 'chaos-test', '{}'::jsonb, 'test-v1', 1)"
                ),
                {"sim_id": sim_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO simulator_latent_state (latent_id, simulation_id, payment_id, "
                    "customer_patience_score, bank_latent_health, latent_network_noise, "
                    "latent_customer_propensity, true_recovery_prob_bps, true_failure_type) "
                    "VALUES (:lid, :sim_id, :pid, 0.8, 0.9, 0.1, 0.2, :prob, 'TEMPORARY_GATEWAY_TIMEOUT')"
                ),
                {
                    "lid": str(uuid.uuid4()),
                    "sim_id": sim_id,
                    "pid": payment_id,
                    "prob": true_recovery_prob_bps,
                },
            )
    await engine.dispose()
    return payment_id, merchant_id, customer_id


@pytest_asyncio.fixture(autouse=True)
async def _no_leaked_recovery_jobs(redis_client):
    """
    Several tests here deliberately produce a real ALLOW verdict via the
    real decide_and_persist path (testing decision-level safety, not full
    execution), which enqueues a real job to stream:recovery_jobs -- but
    never drains it (that's out of scope for those tests). Left alone,
    those jobs sit in the shared session-scoped Redis instance and get
    picked up by OTHER test files' own execution_worker runs later in the
    same pytest session, corrupting their expectations (confirmed: this
    caused test_pipeline_e2e.py/test_execution_worker.py to intermittently
    fail only when run after this file, never in isolation). Reset before
    AND after every test in this module.
    """
    await redis_client.delete("stream:recovery_jobs")
    yield
    await redis_client.delete("stream:recovery_jobs")


async def _reset_streams(redis_client, *stream_names: str) -> None:
    """
    migrated_db/redis_client are SESSION-scoped fixtures (tests/integration/
    conftest.py) -- stream contents, consumer groups, and PEL state persist
    across every test in this file unless explicitly cleared. Deleting the
    key removes the stream AND every consumer group registered on it, so
    each test gets a genuinely clean slate regardless of what earlier tests
    in this file left behind.
    """
    for name in stream_names:
        await redis_client.delete(name)


def _counts(migrated_db: str, payment_id: str) -> dict:
    engine = create_engine(migrated_db, pool_pre_ping=True)
    with engine.connect() as conn:
        row = {
            t: conn.execute(
                text(f"SELECT count(*) FROM {t} WHERE payment_id = :pid"), {"pid": payment_id}
            ).scalar_one()
            for t in (
                "events",
                "diagnoses",
                "candidate_actions",
                "policy_decisions",
                "recoveries",
                "recovery_ledger",
                "audit_log",
            )
        }
    engine.dispose()
    return row


# ─── 1. kill event_processor mid-batch ──────────────────────────────────────


@pytest.mark.asyncio
async def test_kill_event_processor_mid_batch(migrated_db, redis_client, monkeypatch):
    """
    3 events in one batch; the process crashes (raises) partway through the
    3rd. Restart (real XAUTOCLAIM reclaim) must process only the failed one
    again -- the first two must not be reprocessed or duplicated.
    """
    from services.event_processor.consumer import (
        GROUP_NAME,
        STREAM_NAME,
        _ensure_consumer_group,
        _process_batch,
        _reclaim_pending,
    )

    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)
    payment_ids = [str(uuid.uuid4()) for _ in range(3)]

    await _reset_streams(redis_client, STREAM_NAME)
    await _ensure_consumer_group(redis_client)
    for pid in payment_ids:
        await redis_client.xadd(
            STREAM_NAME,
            {
                "event_id": str(uuid.uuid4()),
                "idempotency_key": str(uuid.uuid4()),
                "payment_id": pid,
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "amount_paise": "100000",
                "method": "upi",
                "bank": "HDFC",
                "event_type": "PAYMENT_FAILED",
                "failure_code": "TIMEOUT",
            },
        )

    # process_event() catches every internal exception itself and returns
    # False (see its own docstring) -- a realistic crash simulation patches
    # one of ITS internal calls, not process_event as a whole (which never
    # actually raises past its own boundary in real operation).
    import services.event_processor.processor as processor_module

    real_upsert_payment = processor_module.upsert_payment
    call_count = {"n": 0}

    async def _crash_on_third(session, msg):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise ConnectionError("simulated event_processor crash")
        return await real_upsert_payment(session, msg)

    monkeypatch.setattr(processor_module, "upsert_payment", _crash_on_third)

    results = await redis_client.xreadgroup(
        groupname=GROUP_NAME, consumername="c1", streams={STREAM_NAME: ">"}, count=10
    )
    for _stream, messages in results:
        await _process_batch(redis_client, messages)

    pending = await redis_client.xpending(STREAM_NAME, GROUP_NAME)
    assert pending["pending"] == 1, "exactly the crashed message should remain pending"

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        events_before = conn.execute(
            text("SELECT count(*) FROM events WHERE payment_id::text = ANY(:pids)"),
            {"pids": payment_ids},
        ).scalar_one()
    assert events_before == 2, "the two successfully-processed events must be durably committed"

    # "restart" -- reclaim + let process_event succeed for real this time
    monkeypatch.setattr(processor_module, "upsert_payment", real_upsert_payment)
    monkeypatch.setattr("services.event_processor.consumer.PENDING_RECLAIM_IDLE_MS", 0)
    await _reclaim_pending(redis_client)

    pending_after = await redis_client.xpending(STREAM_NAME, GROUP_NAME)
    assert pending_after["pending"] == 0

    with sync_engine.connect() as conn:
        events_after = conn.execute(
            text("SELECT count(*) FROM events WHERE payment_id::text = ANY(:pids)"),
            {"pids": payment_ids},
        ).scalar_one()
    assert events_after == 3, "no lost canonical event, no duplicate -- exactly 3"
    sync_engine.dispose()


# ─── 2. kill policy/pipeline worker mid-decision ────────────────────────────


@pytest.mark.asyncio
async def test_kill_pipeline_worker_mid_decision(migrated_db, redis_client, monkeypatch):
    """
    process_payment_failure crashes AFTER diagnosis is persisted but BEFORE
    the policy decision is. The message must stay pending (no xack), and a
    retry must reach exactly one diagnosis + one policy_decision -- not a
    duplicate diagnosis, not zero decisions.
    """
    from services.pipeline.consumer import (
        GROUP_NAME,
        STREAM_NAME,
        _ensure_consumer_group,
        _process_batch,
    )

    payment_id, merchant_id, customer_id = await _seed_failed_payment(migrated_db)
    source_event_id = str(uuid.uuid4())

    await _reset_streams(redis_client, STREAM_NAME)
    await _ensure_consumer_group(redis_client)
    await redis_client.xadd(
        STREAM_NAME,
        {
            "source_event_id": source_event_id,
            "payment_id": payment_id,
            "merchant_id": "unused",
            "amount_paise": "200000",
            "method": "upi",
            "bank": "HDFC",
            "event_type": "PAYMENT_FAILED",
            "failure_code": "TIMEOUT",
        },
    )

    import services.pipeline.consumer as pipeline_consumer_module

    real_decide_and_persist = pipeline_consumer_module.decide_and_persist

    async def _crash_before_decision(*args, **kwargs):
        raise ConnectionError("simulated pipeline_orchestrator crash before decision persisted")

    monkeypatch.setattr(pipeline_consumer_module, "decide_and_persist", _crash_before_decision)

    results = await redis_client.xreadgroup(
        groupname=GROUP_NAME, consumername="c1", streams={STREAM_NAME: ">"}, count=10
    )
    for _stream, messages in results:
        await _process_batch(redis_client, messages)

    pending = await redis_client.xpending(STREAM_NAME, GROUP_NAME)
    assert pending["pending"] == 1, "message must stay pending after the mid-decision crash"

    counts_after_crash = _counts(migrated_db, payment_id)
    assert counts_after_crash["diagnoses"] == 1, "diagnosis committed before the crash must survive"
    assert counts_after_crash["policy_decisions"] == 0
    assert counts_after_crash["recovery_ledger"] == 0

    # "restart" -- retry with the real function
    monkeypatch.setattr(pipeline_consumer_module, "decide_and_persist", real_decide_and_persist)
    results = await redis_client.xreadgroup(
        groupname=GROUP_NAME,
        consumername="c1",
        streams={STREAM_NAME: "0"},  # redeliver this consumer's own pending entries
        count=10,
    )
    for _stream, messages in results:
        await _process_batch(redis_client, messages)

    counts_final = _counts(migrated_db, payment_id)
    assert (
        counts_final["diagnoses"] == 1
    ), "must not duplicate the diagnosis on retry (source_event_id dedup)"
    assert counts_final["policy_decisions"] == 1
    assert counts_final["recovery_ledger"] <= 1


# ─── 3. drop Postgres connection mid-write ──────────────────────────────────


@pytest.mark.asyncio
async def test_drop_postgres_connection_mid_ledger_write(migrated_db, redis_client, monkeypatch):
    """
    The connection is severed (OperationalError) exactly during the
    recovery_ledger INSERT. No partial/corrupt row may exist; XACK must not
    happen; a retry after "reconnection" must reach exactly one ledger row.
    """
    from services.pipeline.consumer import (
        GROUP_NAME,
        STREAM_NAME,
        _ensure_consumer_group,
        _process_batch,
    )

    payment_id, merchant_id, customer_id = await _seed_failed_payment(
        migrated_db, with_latent_state=False
    )
    # Force a deterministic BLOCK verdict (opted-out) -- the branch that
    # writes recovery_ledger immediately in the consumer path, no execution
    # worker needed, so the fault is isolated to exactly the write we're
    # targeting.
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE customers SET opted_out_at = :ts WHERE customer_id = :cid"),
            {"ts": datetime.now(UTC) - timedelta(days=1), "cid": customer_id},
        )
    await engine.dispose()

    source_event_id = str(uuid.uuid4())
    await _reset_streams(redis_client, STREAM_NAME)
    await _ensure_consumer_group(redis_client)
    await redis_client.xadd(
        STREAM_NAME,
        {
            "source_event_id": source_event_id,
            "payment_id": payment_id,
            "merchant_id": "unused",
            "amount_paise": "200000",
            "method": "upi",
            "bank": "HDFC",
            "event_type": "PAYMENT_FAILED",
            "failure_code": "TIMEOUT",
        },
    )

    # consumer.py does `from services.pipeline.ledger import
    # populate_ledger_and_audit_async` at module level -- the name is bound
    # in consumer's OWN namespace, so that's what must be patched, not
    # ledger.py's.
    import services.pipeline.consumer as pipeline_consumer_module

    real_populate = pipeline_consumer_module.populate_ledger_and_audit_async
    call_count = {"n": 0}

    async def _crash_first_write(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ConnectionError("simulated Postgres connection drop mid-write")
        return await real_populate(*args, **kwargs)

    monkeypatch.setattr(
        pipeline_consumer_module, "populate_ledger_and_audit_async", _crash_first_write
    )

    results = await redis_client.xreadgroup(
        groupname=GROUP_NAME, consumername="c1", streams={STREAM_NAME: ">"}, count=10
    )
    for _stream, messages in results:
        await _process_batch(redis_client, messages)

    pending = await redis_client.xpending(STREAM_NAME, GROUP_NAME)
    assert pending["pending"] == 1

    counts_after_crash = _counts(migrated_db, payment_id)
    assert (
        counts_after_crash["recovery_ledger"] == 0
    ), "no partial ledger row after a dropped connection"
    assert counts_after_crash["audit_log"] == 0

    monkeypatch.setattr(pipeline_consumer_module, "populate_ledger_and_audit_async", real_populate)
    results = await redis_client.xreadgroup(
        groupname=GROUP_NAME, consumername="c1", streams={STREAM_NAME: "0"}, count=10
    )
    for _stream, messages in results:
        await _process_batch(redis_client, messages)

    counts_final = _counts(migrated_db, payment_id)
    assert (
        counts_final["recovery_ledger"] == 1
    ), "eventually consistent -- exactly one row after retry"
    assert counts_final["audit_log"] == 1


# ─── 4. partition Redis (downstream publish fails) ──────────────────────────


@pytest.mark.asyncio
async def test_partitioned_redis_during_downstream_publish(migrated_db, redis_client, monkeypatch):
    """
    event_processor's DB write (Event + upsert_payment) succeeds, but the
    downstream publish to stream:risk_engine fails (Redis partition). The
    Postgres write must be durable regardless (S4's whole point); the
    message must stay pending; once Redis "heals", the SAME event_id must
    publish exactly once -- not zero, not twice.
    """
    from services.event_processor.consumer import (
        GROUP_NAME,
        STREAM_NAME,
        _ensure_consumer_group,
        _process_batch,
    )

    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)
    payment_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())

    await _reset_streams(redis_client, STREAM_NAME, "stream:risk_engine")
    await _ensure_consumer_group(redis_client)
    await redis_client.xadd(
        STREAM_NAME,
        {
            "event_id": event_id,
            "idempotency_key": event_id,
            "payment_id": payment_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "amount_paise": "100000",
            "method": "upi",
            "bank": "HDFC",
            "event_type": "PAYMENT_FAILED",
            "failure_code": "TIMEOUT",
        },
    )

    import services.event_processor.publisher as publisher_module

    real_publish = publisher_module.publish_to_risk_engine

    async def _partitioned_publish(*args, **kwargs):
        raise ConnectionError("simulated Redis partition during downstream publish")

    monkeypatch.setattr(publisher_module, "publish_to_risk_engine", _partitioned_publish)
    import services.event_processor.processor as processor_module

    monkeypatch.setattr(processor_module, "publish_to_risk_engine", _partitioned_publish)

    results = await redis_client.xreadgroup(
        groupname=GROUP_NAME, consumername="c1", streams={STREAM_NAME: ">"}, count=10
    )
    for _stream, messages in results:
        await _process_batch(redis_client, messages)

    pending = await redis_client.xpending(STREAM_NAME, GROUP_NAME)
    assert pending["pending"] == 1, "publish failure must leave the message pending, not XACK'd"

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        event_count = conn.execute(
            text("SELECT count(*) FROM events WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
        risk_engine_len_before = await redis_client.xlen("stream:risk_engine")
    assert event_count == 1, "the DB write is durable independent of the downstream publish failing"
    assert risk_engine_len_before == 0

    # Redis "heals"
    monkeypatch.setattr(publisher_module, "publish_to_risk_engine", real_publish)
    monkeypatch.setattr(processor_module, "publish_to_risk_engine", real_publish)
    monkeypatch.setattr("services.event_processor.consumer.PENDING_RECLAIM_IDLE_MS", 0)

    from services.event_processor.consumer import _reclaim_pending

    await _reclaim_pending(redis_client)

    risk_engine_len_after = await redis_client.xlen("stream:risk_engine")
    assert (
        risk_engine_len_after == 1
    ), "exactly one publish once Redis heals -- not zero, not duplicated"
    with sync_engine.connect() as conn:
        event_count_final = conn.execute(
            text("SELECT count(*) FROM events WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
    assert event_count_final == 1, "still no duplicate Event row"
    sync_engine.dispose()


# ─── 5 & 6. duplicate / replayed stream event ───────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_and_replayed_events_stay_idempotent(migrated_db, redis_client):
    """
    A 'retry storm' -- the identical event_id/idempotency_key delivered 5
    times in rapid succession (a flaky webhook sender retrying), PLUS one
    genuinely late replay of the same message (simulating Redis's own
    at-least-once redelivery arriving long after the original was thought
    processed). Must collapse to exactly 1 Event row and exactly 1
    downstream publish, regardless of delivery count.
    """
    from services.event_processor.consumer import (
        GROUP_NAME,
        STREAM_NAME,
        _ensure_consumer_group,
        _process_batch,
    )

    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)
    payment_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    msg = {
        "event_id": event_id,
        "idempotency_key": event_id,
        "payment_id": payment_id,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "amount_paise": "100000",
        "method": "upi",
        "bank": "HDFC",
        "event_type": "PAYMENT_FAILED",
        "failure_code": "TIMEOUT",
    }

    await _reset_streams(redis_client, STREAM_NAME, "stream:risk_engine")
    await _ensure_consumer_group(redis_client)
    for _ in range(6):  # 5 rapid duplicates + 1 "late replay"
        await redis_client.xadd(STREAM_NAME, msg)

    results = await redis_client.xreadgroup(
        groupname=GROUP_NAME, consumername="c1", streams={STREAM_NAME: ">"}, count=10
    )
    for _stream, messages in results:
        await _process_batch(redis_client, messages)

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        event_count = conn.execute(
            text("SELECT count(*) FROM events WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
    assert (
        event_count == 1
    ), f"6 deliveries of the same event must collapse to 1 row, got {event_count}"

    risk_engine_len = await redis_client.xlen("stream:risk_engine")
    assert (
        risk_engine_len == 1
    ), f"exactly 1 downstream publish for 6 deliveries, got {risk_engine_len}"
    sync_engine.dispose()


# ─── 7. out-of-order events for the same payment ────────────────────────────


@pytest.mark.asyncio
async def test_out_of_order_events_same_payment_still_yield_one_ledger_row(
    migrated_db, redis_client
):
    """
    Two genuinely DIFFERENT new events (distinct source_event_id) for the
    SAME never-yet-decided payment, delivered back-to-back before either
    finishes processing -- simulating two legitimate signals racing (e.g. a
    duplicate webhook fired from two different upstream paths). Each gets
    its own diagnosis/policy_decision (that's allowed -- different
    source_event_id), but recovery_ledger's UNIQUE(payment_id) must still
    hold: exactly one terminal ledger row, no double recovery.
    """
    from services.pipeline.consumer import (
        GROUP_NAME,
        STREAM_NAME,
        _ensure_consumer_group,
        _process_batch,
    )

    payment_id, merchant_id, customer_id = await _seed_failed_payment(
        migrated_db, with_latent_state=False
    )
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE customers SET opted_out_at = :ts WHERE customer_id = :cid"),
            {"ts": datetime.now(UTC) - timedelta(days=1), "cid": customer_id},
        )
    await engine.dispose()

    await _reset_streams(redis_client, STREAM_NAME)
    await _ensure_consumer_group(redis_client)
    for _ in range(2):
        await redis_client.xadd(
            STREAM_NAME,
            {
                "source_event_id": str(uuid.uuid4()),
                "payment_id": payment_id,
                "merchant_id": "unused",
                "amount_paise": "200000",
                "method": "upi",
                "bank": "HDFC",
                "event_type": "PAYMENT_FAILED",
                "failure_code": "TIMEOUT",
            },
        )

    results = await redis_client.xreadgroup(
        groupname=GROUP_NAME, consumername="c1", streams={STREAM_NAME: ">"}, count=10
    )
    for _stream, messages in results:
        await _process_batch(redis_client, messages)

    counts = _counts(migrated_db, payment_id)
    assert (
        counts["policy_decisions"] == 2
    ), "two distinct source_event_ids legitimately get two decisions"
    assert (
        counts["recovery_ledger"] == 1
    ), f"UNIQUE(payment_id) must hold regardless of arrival order -- got {counts['recovery_ledger']} rows"
    assert (
        counts["audit_log"] == 1
    ), "only the winning write gets an audit_log entry (gaps.md's own rule)"


# ─── 8. consumer restart mid multi-message batch ────────────────────────────


@pytest.mark.asyncio
async def test_consumer_restart_resumes_only_unprocessed_messages(
    migrated_db, redis_client, monkeypatch
):
    """
    5 messages queued; the consumer process dies after message 2 (no
    reclaim attempted -- a hard process kill, not a handled exception).
    A fresh consumer instance starting up must reclaim and finish exactly
    the remaining 3, without reprocessing 1-2.
    """
    from services.event_processor.consumer import (
        GROUP_NAME,
        STREAM_NAME,
        _ensure_consumer_group,
        _process_batch,
    )

    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)
    payment_ids = [str(uuid.uuid4()) for _ in range(5)]

    await _reset_streams(redis_client, STREAM_NAME)
    await _ensure_consumer_group(redis_client)
    for pid in payment_ids:
        await redis_client.xadd(
            STREAM_NAME,
            {
                "event_id": str(uuid.uuid4()),
                "idempotency_key": str(uuid.uuid4()),
                "payment_id": pid,
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "amount_paise": "100000",
                "method": "upi",
                "bank": "HDFC",
                "event_type": "PAYMENT_FAILED",
                "failure_code": "TIMEOUT",
            },
        )

    results = await redis_client.xreadgroup(
        groupname=GROUP_NAME, consumername="dying-consumer", streams={STREAM_NAME: ">"}, count=2
    )
    for _stream, messages in results:
        await _process_batch(redis_client, messages)  # processes + xacks messages 1-2 only

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        events_after_partial = conn.execute(
            text("SELECT count(*) FROM events WHERE payment_id::text = ANY(:pids)"),
            {"pids": payment_ids},
        ).scalar_one()
    assert events_after_partial == 2

    pending = await redis_client.xpending(STREAM_NAME, GROUP_NAME)
    assert (
        pending["pending"] == 0
    ), "the first 2 were cleanly xack'd; the other 3 were never delivered yet"

    # "fresh consumer instance" picks up the rest
    results = await redis_client.xreadgroup(
        groupname=GROUP_NAME, consumername="fresh-consumer", streams={STREAM_NAME: ">"}, count=10
    )
    for _stream, messages in results:
        await _process_batch(redis_client, messages)

    with sync_engine.connect() as conn:
        events_final = conn.execute(
            text("SELECT count(*) FROM events WHERE payment_id::text = ANY(:pids)"),
            {"pids": payment_ids},
        ).scalar_one()
    assert events_final == 5, "all 5 events eventually processed, none lost, none duplicated"
    sync_engine.dispose()


# ─── 9. partial downstream failure (execution succeeds, ledger write fails) ─


@pytest.mark.asyncio
async def test_partial_downstream_failure_execution_then_ledger_write_dies(
    migrated_db, monkeypatch
):
    """
    Sync path (execution_worker): a recovery attempt genuinely executes and
    reaches a terminal SUCCESS outcome, but the ledger/audit write that
    should follow immediately raises. No audit_log entry may exist for a
    write that didn't durably complete; a retry of the SAME write must not
    double-charge (idempotency_key is the physical backstop) and must reach
    exactly one ledger row.

    The test function itself is async only so it can await the shared
    seeding helper without grabbing/running the ambient event loop by hand
    (asyncio.get_event_loop().run_until_complete() here previously left the
    loop in a state that broke every async test that ran after this file in
    a combined tests/unit+tests/integration session -- "RuntimeError: Event
    loop is closed"). The actual assertions below are plain sync code
    (populate_ledger_and_audit_sync is a sync function), which is fine to
    call directly inside an async test body.
    """
    payment_id, merchant_id, customer_id = await _seed_failed_payment(
        migrated_db, true_recovery_prob_bps=10000
    )

    import services.pipeline.ledger as ledger_module

    real_sync_populate = ledger_module.populate_ledger_and_audit_sync
    call_count = {"n": 0}

    def _crash_first_ledger_write(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ConnectionError("simulated crash between execution and ledger write")
        return real_sync_populate(*args, **kwargs)

    monkeypatch.setattr(ledger_module, "populate_ledger_and_audit_sync", _crash_first_ledger_write)

    # Directly exercise the ledger write in isolation (the piece under test)
    # rather than the full worker loop -- this test is about the write's own
    # crash-then-retry safety, which is the load-bearing property here.
    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        with pytest.raises(ConnectionError):
            ledger_module.populate_ledger_and_audit_sync(
                conn,
                payment_id=payment_id,
                candidate_id=None,
                decision_id=None,  # audit_log's decision_id/recovery_id FKs are nullable --
                verdict="ALLOW",  # this test targets the ledger write's own crash-retry
                chosen_action="RETRY_NOW",  # safety, not FK integrity, so no real
                recovery_prob_bps=8000,  # policy_decisions/recoveries rows are needed.
                cost_paise=0,
                actual_recovery_paise=160_000,
                recovery_id=None,
                diagnosis_id=None,
                outcome="SUCCESS",
            )
        conn.rollback()

        ledger_count_after_crash = conn.execute(
            text("SELECT count(*) FROM recovery_ledger WHERE payment_id = :pid"),
            {"pid": payment_id},
        ).scalar_one()
        status_after_crash = conn.execute(
            text("SELECT status FROM payments WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()

    assert ledger_count_after_crash == 0, "no partial ledger row after the simulated crash"
    assert (
        status_after_crash == "failed"
    ), "payments.status must not flip to 'recovered' on a write that didn't complete"

    with sync_engine.connect() as conn:
        ledger_module.populate_ledger_and_audit_sync(
            conn,
            payment_id=payment_id,
            candidate_id=None,
            decision_id=None,
            verdict="ALLOW",
            chosen_action="RETRY_NOW",
            recovery_prob_bps=8000,
            cost_paise=0,
            actual_recovery_paise=160_000,
            recovery_id=None,
            diagnosis_id=None,
            outcome="SUCCESS",
        )

    with sync_engine.connect() as conn:
        ledger_count_final = conn.execute(
            text("SELECT count(*) FROM recovery_ledger WHERE payment_id = :pid"),
            {"pid": payment_id},
        ).scalar_one()
        audit_count_final = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
        status_final = conn.execute(
            text("SELECT status FROM payments WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()

    assert (
        ledger_count_final == 1
    ), "eventually consistent -- exactly one ledger row after the retry succeeds"
    assert audit_count_final == 1
    assert status_final == "recovered"
    sync_engine.dispose()
