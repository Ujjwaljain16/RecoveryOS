"""
Event Processor — Core Processing Logic
=======================================
Orchestrates the DB write and downstream publish for a single event.

Idempotency contract (at-least-once delivery, idempotent sink):
  Redis Streams → at-least-once delivery (consumer retries on failure)
  Postgres      → idempotent sink (ON CONFLICT DO NOTHING is the backstop)

Processing order:
  1. Upsert Payment (idempotent)
  2. Insert Event (idempotent — deduplication gate)
  3. Commit DB transaction
  4. Publish downstream (if event was new)
  → XACK only after all of the above succeed

Failure modes:
  - DB unavailable: exception propagates, no XACK → message stays pending.
  - Publisher fails: exception propagates, no XACK → message stays pending.
    On retry, DB write is idempotent (no duplicate), publish retried.
  - Crash after DB commit but before XACK: message re-delivered on restart,
    DB write is idempotent → no duplicate in Postgres.
"""

from __future__ import annotations

import logging
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from services.event_processor.publisher import publish_to_risk_engine
from services.event_processor.repository import insert_event_idempotent, upsert_payment

logger = logging.getLogger(__name__)


async def process_event(
    msg: dict[str, Any],
    session: AsyncSession,
    redis: aioredis.Redis,
) -> bool:
    """
    Process a single event from the Redis stream.

    Returns True if processed (or already seen and skipped), False on error.
    Caller should XACK on True, leave pending on False.
    """
    event_id = msg.get("event_id", "<unknown>")
    payment_id = msg.get("payment_id", "<unknown>")

    try:
        # ── Step 1: Upsert Payment ───────────────────────────────────────────
        await upsert_payment(session, msg)

        # ── Step 2: Insert Event (idempotent) ────────────────────────────────
        is_new = await insert_event_idempotent(session, msg)

        # ── Step 3: Commit DB ────────────────────────────────────────────────
        await session.commit()

        if is_new:
            # ── Step 4: Publish downstream (only for new events) ─────────────
            await publish_to_risk_engine(redis, event_id, msg)
            logger.info(
                "[Processor] ✓ event_id=%s payment_id=%s type=%s",
                event_id,
                payment_id,
                msg.get("event_type"),
            )
        else:
            logger.info("[Processor] ↩ Duplicate skipped: event_id=%s", event_id)

        return True

    except Exception as exc:
        await session.rollback()
        logger.error(
            "[Processor] ✗ Failed to process event_id=%s payment_id=%s: %s",
            event_id,
            payment_id,
            exc,
            exc_info=True,
        )
        return False
