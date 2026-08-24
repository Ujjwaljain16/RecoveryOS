"""
Event Processor — Downstream Publisher
======================================
Publishes processed events to downstream streams for Phase 4 consumers.

Current downstream targets:
  - stream:risk_engine  → Risk Engine (Phase 4 anomaly detection)

Architecture note: Redis Streams = at-least-once delivery.
  This publisher runs AFTER the DB commit (Postgres is the durable ledger).
  If publish fails, the consumer does NOT XACK — the message stays pending
  and will be retried, keeping the system consistent.
"""
from __future__ import annotations

import logging
from typing import Any

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

STREAM_RISK_ENGINE = "stream:risk_engine"
STREAM_MAXLEN = 50_000


async def publish_to_risk_engine(
    redis: aioredis.Redis,
    event_id: str,
    msg: dict[str, Any],
) -> str:
    """
    Publish a processed event downstream to the risk engine stream.
    Returns the Redis stream message ID.
    """
    downstream_msg = {
        "source_event_id": event_id,
        "payment_id": msg["payment_id"],
        "merchant_id": msg["merchant_id"],
        "amount_paise": str(msg["amount_paise"]),
        "method": msg["method"],
        "bank": msg.get("bank") or "",
        "event_type": msg["event_type"],
        "failure_code": msg.get("failure_code") or "",
    }

    stream_id: str = await redis.xadd(
        STREAM_RISK_ENGINE,
        downstream_msg,
        maxlen=STREAM_MAXLEN,
        approximate=True,
    )
    logger.debug(
        "[Publisher] Published to %s: event_id=%s stream_id=%s",
        STREAM_RISK_ENGINE,
        event_id,
        stream_id,
    )
    return stream_id
