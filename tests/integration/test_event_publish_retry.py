"""
Task S4 (pre-Phase-8 audit): a publish failure must actually be retried on
redelivery, not silently skipped forever.

Before this fix, process_event() gated the publish call on
insert_event_idempotent()'s is_new flag. Once the Event row committed
(step 3), a redelivered message for the SAME event always found is_new=False
and skipped the publish step entirely on every subsequent attempt — even
though the publish itself never actually succeeded the first time. The fix
gates the publish on a separate, INSERT-only event_publications table
(recoveryos/models.py:EventPublication) recording only that a publish
actually completed.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from services.event_processor.processor import process_event
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


def _make_msg(payment_id: str, merchant_id: str, customer_id: str, event_id: str) -> dict:
    return {
        "event_id": event_id,
        "idempotency_key": event_id,
        "payment_id": payment_id,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "amount_paise": "50000",
        "method": "upi",
        "bank": "HDFC",
        "event_type": "PAYMENT_FAILED",
        "failure_code": "TIMEOUT",
    }


@pytest.mark.asyncio
async def test_publish_failure_is_retried_on_redelivery(migrated_db, redis_client, monkeypatch):
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    payment_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    msg = _make_msg(payment_id, merchant_id, customer_id, event_id)

    engine = create_async_engine(to_async_url(migrated_db))

    import services.event_processor.processor as processor_module

    publish_calls = {"count": 0}
    real_publish = processor_module.publish_to_risk_engine

    async def _publish_fails_once(redis, evt_id, message):
        publish_calls["count"] += 1
        if publish_calls["count"] == 1:
            raise ConnectionError("simulated transient Redis failure during publish")
        return await real_publish(redis, evt_id, message)

    monkeypatch.setattr(processor_module, "publish_to_risk_engine", _publish_fails_once)

    # ── First delivery: DB write succeeds and commits, publish raises. ────
    async with AsyncSession(engine) as session:
        ok_first = await process_event(msg, session, redis_client)
    assert ok_first is False, "process_event must report failure when publish raises"
    assert publish_calls["count"] == 1

    from sqlalchemy import text

    async with engine.begin() as conn:
        event_count = (
            await conn.execute(
                text("SELECT count(*) FROM events WHERE event_id = :eid"), {"eid": event_id}
            )
        ).scalar_one()
        published_count = (
            await conn.execute(
                text("SELECT count(*) FROM event_publications WHERE event_id = :eid"),
                {"eid": event_id},
            )
        ).scalar_one()
    assert event_count == 1, "the DB write itself must have succeeded despite the publish failure"
    assert published_count == 0, "must not be marked published when the publish actually failed"

    # ── Redelivery: same message, same event_id already in Postgres. ──────
    async with AsyncSession(engine) as session:
        ok_second = await process_event(msg, session, redis_client)
    assert ok_second is True

    assert publish_calls["count"] == 2, (
        "the SECOND attempt must have genuinely retried the publish call -- "
        f"got {publish_calls['count']} total publish attempts, expected 2. Before this fix, "
        "insert_event_idempotent's is_new=False on redelivery caused the publish to be "
        "skipped entirely, silently dropping this event's downstream notification forever."
    )

    async with engine.begin() as conn:
        published_count_after = (
            await conn.execute(
                text("SELECT count(*) FROM event_publications WHERE event_id = :eid"),
                {"eid": event_id},
            )
        ).scalar_one()
    assert published_count_after == 1

    stream_messages = await redis_client.xrange("stream:risk_engine")
    matching = [m for _id, m in stream_messages if m.get("source_event_id") == event_id]
    assert len(matching) == 1, (
        f"expected exactly one stream:risk_engine message for event_id={event_id}, "
        f"found {len(matching)} -- the retried publish must have actually landed."
    )

    await engine.dispose()
