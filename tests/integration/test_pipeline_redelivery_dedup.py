"""
Task S1 (pre-Phase-8 audit): a redelivered stream:risk_engine message must
not create a duplicate recovery_ledger row.

The real scenario this reproduces: services/pipeline/consumer.py's
_process_batch calls process_payment_failure() to completion (which writes
recovery_ledger via populate_ledger_and_audit_async and commits) and THEN
calls redis.xack() -- if THAT call raises (a transient Redis blip, nothing
to do with the already-successful DB write), the message is never XACK'd
and stays in the consumer group's Pending Entry List. XAUTOCLAIM later
reclaims it and _process_batch reprocesses the SAME message from scratch,
including a second populate_ledger_and_audit_async call for the same
payment. TRD §7's incremental-revenue number is a raw SQL SUM() over
recovery_ledger -- a duplicate row silently inflates it.

This test forces exactly that sequence: xack raises on the first delivery,
the message is reclaimed via the real XAUTOCLAIM path (not a bare double
call to process_payment_failure -- the actual consumer-group mechanics),
and process_payment_failure runs a second time for the same payment before
asserting recovery_ledger has exactly one row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import seed_merchant_and_customer, to_async_url


async def _seed_opted_out_payment(migrated_db: str) -> str:
    """
    A payment whose customer has already opted out -- OptOutRule fails for
    EVERY candidate action, guaranteeing verdict=BLOCK deterministically
    (no dependence on propensity/EVI numbers), which is exactly the branch
    of process_payment_failure that writes recovery_ledger immediately.
    """
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    engine = create_async_engine(to_async_url(migrated_db))
    payment_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE customers SET opted_out_at = :ts WHERE customer_id = :cid"),
            {"ts": datetime.now(UTC) - timedelta(days=1), "cid": customer_id},
        )
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, 150000, 'upi', 'HDFC', 'failed', 'TIMEOUT', 'TEMPORARY', "
                "true, :ts, :ts)"
            ),
            {"pid": payment_id, "mid": merchant_id, "cid": customer_id, "ts": datetime.now(UTC)},
        )
    await engine.dispose()
    return payment_id


@pytest.mark.asyncio
async def test_redelivered_message_after_xack_failure_does_not_duplicate_ledger_row(
    migrated_db, redis_client, monkeypatch
):
    from services.pipeline.consumer import (
        GROUP_NAME,
        STREAM_NAME,
        _ensure_consumer_group,
        _process_batch,
        _reclaim_pending,
    )

    payment_id = await _seed_opted_out_payment(migrated_db)
    source_event_id = str(uuid.uuid4())

    await _ensure_consumer_group(redis_client)
    await redis_client.xadd(
        STREAM_NAME,
        {
            "source_event_id": source_event_id,
            "payment_id": payment_id,
            "merchant_id": "unused",
            "amount_paise": "150000",
            "method": "upi",
            "bank": "HDFC",
            "event_type": "PAYMENT_FAILED",
            "failure_code": "TIMEOUT",
        },
    )

    # ── First delivery: process_payment_failure fully succeeds (ledger row
    # written, committed), but the xack call itself raises -- exactly the
    # failure this task targets, not a failure in the actual processing.
    real_xack = redis_client.xack
    xack_calls = {"count": 0}

    async def _xack_fails_once(*args, **kwargs):
        xack_calls["count"] += 1
        if xack_calls["count"] == 1:
            raise ConnectionError("simulated transient Redis failure during xack")
        return await real_xack(*args, **kwargs)

    monkeypatch.setattr(redis_client, "xack", _xack_fails_once)

    results = await redis_client.xreadgroup(
        groupname=GROUP_NAME, consumername="test-consumer-1", streams={STREAM_NAME: ">"}, count=10
    )
    for _stream_name, messages in results:
        await _process_batch(redis_client, messages)

    # Confirm the message is genuinely still pending (xack really didn't happen).
    pending = await redis_client.xpending(STREAM_NAME, GROUP_NAME)
    assert (
        pending["pending"] == 1
    ), "message should still be in the PEL after the simulated xack failure"

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        count_after_first_delivery = conn.execute(
            text("SELECT count(*) FROM recovery_ledger WHERE payment_id = :pid"),
            {"pid": payment_id},
        ).scalar_one()
    assert (
        count_after_first_delivery == 1
    ), "first delivery must have written exactly one ledger row"

    # ── Reclaim via the REAL XAUTOCLAIM path (not a bare second call) --
    # xack now succeeds, so this genuinely completes the redelivery.
    monkeypatch.setattr(
        "services.pipeline.consumer.PENDING_RECLAIM_IDLE_MS", 0
    )  # reclaim immediately, don't wait out the real 5s idle threshold
    await _reclaim_pending(redis_client)

    pending_after_reclaim = await redis_client.xpending(STREAM_NAME, GROUP_NAME)
    assert pending_after_reclaim["pending"] == 0, "reclaimed message should now be xack'd"

    with sync_engine.connect() as conn:
        final_count = conn.execute(
            text("SELECT count(*) FROM recovery_ledger WHERE payment_id = :pid"),
            {"pid": payment_id},
        ).scalar_one()

    assert final_count == 1, (
        f"redelivery after an xack failure must NOT duplicate the recovery_ledger row -- "
        f"found {final_count} rows for payment_id={payment_id}. TRD §7's incremental-revenue "
        f"SUM() would silently double-count this payment."
    )

    sync_engine.dispose()


@pytest.mark.asyncio
async def test_redelivered_message_does_not_duplicate_diagnosis_or_candidate_rows(
    migrated_db, redis_client, monkeypatch
):
    """Same redelivery scenario, checking the lower-severity but still-real
    diagnoses/candidate_actions/policy_decisions duplication the audit also
    flagged (Task S1, point 3)."""
    from services.pipeline.consumer import (
        GROUP_NAME,
        STREAM_NAME,
        _ensure_consumer_group,
        _process_batch,
        _reclaim_pending,
    )

    payment_id = await _seed_opted_out_payment(migrated_db)
    source_event_id = str(uuid.uuid4())

    await _ensure_consumer_group(redis_client)
    await redis_client.xadd(
        STREAM_NAME,
        {
            "source_event_id": source_event_id,
            "payment_id": payment_id,
            "merchant_id": "unused",
            "amount_paise": "150000",
            "method": "upi",
            "bank": "HDFC",
            "event_type": "PAYMENT_FAILED",
            "failure_code": "TIMEOUT",
        },
    )

    real_xack = redis_client.xack
    xack_calls = {"count": 0}

    async def _xack_fails_once(*args, **kwargs):
        xack_calls["count"] += 1
        if xack_calls["count"] == 1:
            raise ConnectionError("simulated transient Redis failure during xack")
        return await real_xack(*args, **kwargs)

    monkeypatch.setattr(redis_client, "xack", _xack_fails_once)

    results = await redis_client.xreadgroup(
        groupname=GROUP_NAME, consumername="test-consumer-1", streams={STREAM_NAME: ">"}, count=10
    )
    for _stream_name, messages in results:
        await _process_batch(redis_client, messages)

    monkeypatch.setattr("services.pipeline.consumer.PENDING_RECLAIM_IDLE_MS", 0)
    await _reclaim_pending(redis_client)

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        diagnosis_count = conn.execute(
            text("SELECT count(*) FROM diagnoses WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
        candidate_count = conn.execute(
            text("SELECT count(*) FROM candidate_actions WHERE payment_id = :pid"),
            {"pid": payment_id},
        ).scalar_one()
        policy_decision_count = conn.execute(
            text("SELECT count(*) FROM policy_decisions WHERE payment_id = :pid"),
            {"pid": payment_id},
        ).scalar_one()

    assert diagnosis_count == 1, f"expected exactly 1 diagnosis, got {diagnosis_count}"
    assert (
        candidate_count == 6
    ), f"expected exactly 6 candidate_actions (one per action type), got {candidate_count}"
    assert (
        policy_decision_count == 1
    ), f"expected exactly 1 policy_decision, got {policy_decision_count}"

    sync_engine.dispose()
