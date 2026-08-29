"""
Retry Scheduler — Task REPLAN1, the continuous-replanning half of the
RETRY_LATER path.

Async, like services/pipeline/consumer.py, NOT sync like
workers/execution_worker.py: this worker re-runs diagnose_and_persist /
decide_and_persist, both of which are already async (asyncpg + httpx to
the LLM), so there is no reason to force a sync/async bridge here the way
execution_worker deliberately avoids one for its own (unrelated) reasons.

Polls scheduled_reevaluations for rows whose time has come, atomically
claims each one (services/recovery_engine/scheduling.claim_reevaluation --
PENDING -> FIRED, WHERE status='PENDING' is the whole concurrency
mechanism, no advisory lock needed), then re-runs
services.pipeline.consumer.process_payment_failure for that payment with a
FRESH source_event_id (fired_source_event_id) -- a genuine re-evaluation
against current anomaly/cooldown/attempt-number state, not a replay of the
stale decision that produced this row.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import text

from recoveryos import clock
from recoveryos.database import get_app_session_factory
from services.pipeline.consumer import process_payment_failure
from services.recovery_engine.scheduling import claim_reevaluation, fetch_due_reevaluations

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5


async def _fetch_bank(payment_id: str) -> str | None:
    async with get_app_session_factory()() as session:
        row = (
            await session.execute(
                text("SELECT bank FROM payments WHERE payment_id = :pid"), {"pid": payment_id}
            )
        ).first()
        return row[0] if row else None


async def _process_one(row: dict, redis_client) -> None:
    fired_source_event_id = str(uuid.uuid4())
    async with get_app_session_factory()() as session:
        won = await claim_reevaluation(
            session, row["reevaluation_id"], fired_source_event_id, clock.utcnow()
        )
    if not won:
        # Another scheduler instance claimed it first -- not an error.
        return

    bank = await _fetch_bank(row["payment_id"])
    await process_payment_failure(
        row["payment_id"], bank, redis_client, source_event_id=fired_source_event_id
    )


async def run_once(redis_client) -> int:
    """One poll cycle. Returns how many due rows were processed (test hook)."""
    async with get_app_session_factory()() as session:
        due = await fetch_due_reevaluations(session, clock.utcnow())

    for row in due:
        try:
            await _process_one(row, redis_client)
        except Exception:
            logger.exception(
                "[RetryScheduler] failed re-evaluating reevaluation_id=%s (row stays FIRED, "
                "will not be retried automatically -- same as any other pipeline failure mode)",
                row["reevaluation_id"],
            )
    return len(due)


async def run_scheduler(
    redis_client,
    *,
    max_iterations: int | None = None,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
) -> None:
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        await run_once(redis_client)
        if max_iterations is None or iterations < max_iterations:
            await asyncio.sleep(poll_interval_seconds)


async def main() -> None:
    import logging as _logging

    import redis.asyncio as aioredis
    from prometheus_client import start_http_server

    from recoveryos.config import get_settings

    _logging.basicConfig(
        level=_logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = get_settings()
    # TRD §10: retry_scheduler re-enters the same instrumented
    # diagnose_and_persist/decide_and_persist/ledger.py code paths as
    # pipeline_orchestrator when a deferred RETRY_LATER fires -- its own
    # scrape port, not a shared one.
    start_http_server(settings.prometheus_port)
    redis_client = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    try:
        await run_scheduler(redis_client)
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
