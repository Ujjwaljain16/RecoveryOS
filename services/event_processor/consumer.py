"""
Event Processor — Redis Streams Consumer
========================================
Manages the Redis Streams consumer group mechanics.

Delivery guarantee: at-least-once (Redis Streams design).
Idempotency: enforced at the processor/repository layer (not here).

Consumer group pattern:
  - Group name: cg_event_processor
  - Consumer name: hostname-based (unique per process instance)
  - On startup: creates the consumer group if it doesn't exist (MKSTREAM).
  - Main loop: XREADGROUP with COUNT=10, BLOCK=1000ms.
  - PEL (Pending Entry List) reprocessing: on startup, reclaims messages
    that were delivered but not XACK'd by any consumer (i.e. previously crashed).
  - XACK only after successful processing — message stays pending on failure.

Consumer restart recovery:
  On restart, the consumer first drains the PEL by calling XAUTOCLAIM with a
  min-idle-time of 0 (reclaims any pending messages from the last consumer run).
  This handles the "crash after DB commit, before XACK" scenario.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket

import redis.asyncio as aioredis

from recoveryos.database import get_app_engine
from recoveryos.metrics import stream_backlog_depth
from services.event_processor.processor import process_event

logger = logging.getLogger(__name__)

STREAM_NAME = "stream:payment_failed"
GROUP_NAME = "cg_event_processor"
CONSUMER_NAME = f"{socket.gethostname()}-{os.getpid()}"
BATCH_SIZE = 10
BLOCK_MS = 1000  # block up to 1s waiting for new messages
PENDING_RECLAIM_IDLE_MS = 5000  # reclaim messages idle > 5s in PEL


async def _record_backlog(redis: aioredis.Redis) -> None:
    """
    Domain Audit finding #4: this is the very first ingestion stage in
    the whole pipeline, and previously had zero Prometheus instrumentation
    at all -- if PAYMENT_FAILED volume outpaced this consumer, nothing in
    the stack would surface it until symptoms appeared much further
    downstream. `lag` (XINFO GROUPS' own field) is entries in the stream
    that have never been delivered to ANY consumer in cg_event_processor
    -- the direct "are we falling behind the incoming stream" signal, not
    a throughput count that says nothing about backlog. Best-effort: a
    transient Redis error here must never take down the consumer loop
    over an observability side-channel.
    """
    try:
        groups = await redis.xinfo_groups(STREAM_NAME)
        for group in groups:
            if group.get("name") == GROUP_NAME:
                lag = group.get("lag")
                if lag is not None:
                    stream_backlog_depth.labels(stream=STREAM_NAME, group=GROUP_NAME).set(lag)
                break
    except Exception:
        logger.exception("[Consumer] failed to record stream backlog (non-fatal)")


async def _ensure_consumer_group(redis: aioredis.Redis) -> None:
    """Create the consumer group if it doesn't exist."""
    try:
        await redis.xgroup_create(STREAM_NAME, GROUP_NAME, id="0", mkstream=True)
        logger.info("[Consumer] Created consumer group %s on %s", GROUP_NAME, STREAM_NAME)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" in str(e):
            logger.debug("[Consumer] Consumer group %s already exists", GROUP_NAME)
        else:
            raise


async def _process_batch(
    redis: aioredis.Redis,
    messages: list[tuple[str, dict[str, str]]],
) -> None:
    """Process a batch of messages from XREADGROUP, XACK each successful one."""
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = get_app_engine()

    for stream_msg_id, raw_msg in messages:
        async with AsyncSession(engine) as session:
            ok = await process_event(raw_msg, session, redis)

        if ok:
            await redis.xack(STREAM_NAME, GROUP_NAME, stream_msg_id)
            logger.debug("[Consumer] XACK'd: %s", stream_msg_id)
        else:
            # Leave in PEL — will be retried on next startup or by XAUTOCLAIM
            logger.warning("[Consumer] Left pending (will retry): %s", stream_msg_id)


async def _reclaim_pending(redis: aioredis.Redis) -> None:
    """
    On startup: reclaim and reprocess messages that were delivered but not XACK'd
    (i.e. orphaned by a previous consumer crash).

    Uses XAUTOCLAIM to efficiently find and take ownership of idle pending messages.
    """
    logger.info("[Consumer] Checking for pending messages to reclaim...")
    while True:
        result = await redis.xautoclaim(
            STREAM_NAME,
            GROUP_NAME,
            CONSUMER_NAME,
            min_idle_time=PENDING_RECLAIM_IDLE_MS,
            start_id="0-0",
            count=BATCH_SIZE,
        )
        # result = (next_start_id, messages, deleted_ids)
        next_id, messages, _ = result

        if not messages:
            break

        logger.info("[Consumer] Reclaiming %d pending messages...", len(messages))
        await _process_batch(redis, messages)

        if next_id == "0-0":
            break


async def run_consumer(redis: aioredis.Redis) -> None:
    """
    Main consumer loop. Runs indefinitely until cancelled.

    Startup sequence:
      1. Ensure consumer group exists.
      2. Reclaim any pending messages from a prior crashed instance.
      3. Enter main XREADGROUP loop.
    """
    await _ensure_consumer_group(redis)
    await _reclaim_pending(redis)

    logger.info(
        "[Consumer] Starting consumer loop. stream=%s group=%s consumer=%s",
        STREAM_NAME,
        GROUP_NAME,
        CONSUMER_NAME,
    )

    while True:
        try:
            results = await redis.xreadgroup(
                groupname=GROUP_NAME,
                consumername=CONSUMER_NAME,
                streams={STREAM_NAME: ">"},  # ">" = only deliver new (undelivered) messages
                count=BATCH_SIZE,
                block=BLOCK_MS,
            )

            await _record_backlog(redis)

            if not results:
                # No new messages in this poll window — normal.
                continue

            for _stream_name, messages in results:
                await _process_batch(redis, messages)

        except asyncio.CancelledError:
            logger.info("[Consumer] Shutdown requested. Exiting gracefully.")
            break
        except Exception as exc:
            logger.error("[Consumer] Unexpected error: %s", exc, exc_info=True)
            await asyncio.sleep(1)  # brief backoff before retrying


async def main() -> None:
    """Entrypoint for running the consumer as a standalone process."""
    import redis.asyncio as aioredis
    from prometheus_client import start_http_server

    from recoveryos.config import get_settings

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    settings = get_settings()
    # Domain Audit finding #4: previously the only one of the four
    # consumer-group processes with zero Prometheus instrumentation.
    start_http_server(settings.prometheus_port)
    redis_client = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )

    try:
        await run_consumer(redis_client)
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
