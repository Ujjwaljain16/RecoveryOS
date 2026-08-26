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
  4. Publish downstream, gated on event_publications (has this event_id
     actually been published before?) — NOT on step 2's is_new flag.
  → XACK only after all of the above succeed

Failure modes:
  - DB unavailable: exception propagates, no XACK → message stays pending.
  - Publisher fails: exception propagates, no XACK → message stays pending.
    On retry, the DB write is idempotent (no duplicate) — critically, the
    publish step is genuinely re-attempted too, because it's gated on
    event_publications, a separate fact recorded only once publish actually
    succeeds, not on whether the Event row itself was new. (Task S4,
    pre-Phase-8 audit: this used to be gated on is_new, which meant a
    publish failure after a successful Event commit was retried at the DB
    layer forever but the publish itself was skipped on every retry,
    silently dropping that event's downstream notification permanently.)
  - Crash after DB commit but before XACK: message re-delivered on restart,
    DB write is idempotent → no duplicate in Postgres, and the publish
    genuinely retries per the above.
"""

from __future__ import annotations

import logging
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from services.event_processor.publisher import publish_to_risk_engine
from services.event_processor.repository import (
    insert_event_idempotent,
    is_event_published,
    mark_event_published,
    upsert_payment,
)

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
        resolved_event_id, is_new = await insert_event_idempotent(session, msg)

        # ── Step 3: Commit DB ────────────────────────────────────────────────
        await session.commit()

        # ── Step 4: Publish downstream — gated on event_publications, NOT
        # is_new (Task S4, pre-Phase-8 audit). is_new only tells us whether
        # the Event row itself was newly inserted; a prior attempt could have
        # inserted that row and then failed BEFORE publish_to_risk_engine
        # succeeded (a transient Redis blip has nothing to do with the DB
        # write's own idempotency). Checking event_publications instead means
        # a redelivered message for an already-stored-but-never-published
        # event still gets its publish retried, not silently skipped forever.
        if not await is_event_published(session, resolved_event_id):
            await publish_to_risk_engine(redis, resolved_event_id, msg)
            await mark_event_published(session, resolved_event_id)
            await session.commit()
            logger.info(
                "[Processor] ✓ event_id=%s payment_id=%s type=%s is_new=%s",
                resolved_event_id,
                payment_id,
                msg.get("event_type"),
                is_new,
            )
        else:
            logger.info("[Processor] ↩ Already published, skipping: event_id=%s", resolved_event_id)

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
