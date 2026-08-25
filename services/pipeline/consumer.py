"""
Pipeline orchestrator — TRD §1.4's full data flow trace, wired end to end:

    PAYMENT_FAILED event (stream:risk_engine, published by event_processor)
        -> risk engine (Phase 4 anomaly detector, bank-scoped)
        -> diagnosis (Phase 4 AI Diagnoser + fallback)
        -> recovery engine + policy engine (Phase 5, services/recovery_engine/orchestrator.py)
        -> action queue (stream:recovery_jobs, enqueued by orchestrator.decide_and_persist)
        -> worker (Phase 6, workers/execution_worker.py) -- async, NOT awaited here
        -> outcome -> recovery_ledger -> audit_log

The correlation ID threading every one of these tables together is simply
payment_id — every table in this chain (events, diagnoses, candidate_actions,
policy_decisions, recoveries, recovery_ledger, audit_log) already carries it
as a real FK (TRD §2's schema, not a new column this phase invents).

Terminal-row responsibility split (why this consumer sometimes writes
recovery_ledger/audit_log itself and sometimes doesn't):
  - verdict != ALLOW, or verdict == ALLOW but chosen_action == DO_NOTHING:
    no execution job is ever enqueued, so THIS consumer writes the
    terminal ledger/audit row immediately -- nothing downstream ever will.
  - verdict == ALLOW and an action actually executes: this consumer does
    NOT write the terminal row. workers/execution_worker.py does, once the
    job reaches a real SUCCESS/FAILED outcome (a PENDING outcome is not
    terminal yet).

Same consumer-group resilience pattern as event_processor and
execution_worker (XREADGROUP + XAUTOCLAIM) -- the third instance of the
same proven mechanism in this codebase, not a fourth different one.
"""

from __future__ import annotations

import logging
import os
import socket
from datetime import UTC, datetime

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services.diagnosis_engine.diagnoser import diagnose_and_persist
from services.pipeline.ledger import populate_ledger_and_audit_async
from services.recovery_engine.orchestrator import decide_and_persist
from services.risk_engine.anomaly import compute_anomaly_window, persist_anomaly_window

logger = logging.getLogger(__name__)

STREAM_NAME = "stream:risk_engine"
GROUP_NAME = "cg_pipeline_orchestrator"
CONSUMER_NAME = f"{socket.gethostname()}-{os.getpid()}"
BATCH_SIZE = 10
BLOCK_MS = 1000
PENDING_RECLAIM_IDLE_MS = 5000

# Only PAYMENT_FAILED events start a recovery decision chain -- other event
# types (PAYMENT_CREATED, PAYMENT_SUCCESS, ...) are published to the same
# downstream stream by event_processor but have nothing to diagnose/decide.
TRIGGERING_EVENT_TYPE = "PAYMENT_FAILED"


async def _run_anomaly_detection_for_payment(session: AsyncSession, bank: str | None) -> None:
    """Risk-engine step: refresh this bank's current-bucket anomaly window
    before diagnosis/policy read it. Best-effort -- a detector hiccup must
    not block the rest of the chain; diagnosis/policy simply see whatever
    anomaly_windows state already existed (or none) if this fails."""
    if bank is None:
        return
    try:
        bucket_start = datetime.now(UTC)
        result = await compute_anomaly_window(session, "bank", bank, bucket_start)
        await persist_anomaly_window(session, result)
    except Exception:
        logger.exception("[Pipeline] anomaly detection step failed for bank=%s (continuing)", bank)


async def _fetch_chosen_candidate(
    session: AsyncSession, payment_id: str, action_type: str
) -> dict | None:
    row = (
        (
            await session.execute(
                text(
                    "SELECT candidate_id, cost_paise, recovery_prob_bps FROM candidate_actions "
                    "WHERE payment_id = :pid AND action_type = :action ORDER BY created_at DESC LIMIT 1"
                ),
                {"pid": payment_id, "action": action_type},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def process_payment_failure(payment_id: str, bank: str | None, redis: aioredis.Redis) -> None:
    """
    One full decision chain for one failed payment. Raises on genuine
    infrastructure failure (DB unreachable, etc.) so the caller leaves the
    stream message pending for retry -- does NOT raise merely because the
    AI Diagnoser was unavailable, since diagnose_and_persist already
    resolves that internally via the deterministic fallback (Phase 4).
    """
    from recoveryos.database import get_app_session_factory
    from services.pipeline.baseline import compute_and_persist_baseline_run

    async with get_app_session_factory()() as session:
        await _run_anomaly_detection_for_payment(session, bank)
        # Computed here, unconditionally, BEFORE the ALLOW/DO_NOTHING branch
        # below -- whichever path ends up writing the terminal ledger row
        # (this consumer immediately, or execution_worker later) needs a
        # baseline_runs row to already exist; execution_worker's sync path
        # only READS baseline_runs, it never computes one.
        await compute_and_persist_baseline_run(session, payment_id)

    # diagnose_and_persist opens its own diagnoser_role + app_role sessions
    # internally -- never raises merely because the LLM is unreachable/
    # times out (that's exactly what the fallback path is for).
    diagnosis = await diagnose_and_persist(payment_id)
    diagnosis_id = diagnosis.diagnosis_id if diagnosis is not None else None

    result = await decide_and_persist(payment_id, redis_client=redis)

    if result["verdict"] != "ALLOW" or result["chosen_action"] == "DO_NOTHING":
        # No execution job was enqueued for this payment -- this IS the
        # terminal state. Write recovery_ledger + audit_log now.
        async with get_app_session_factory()() as session:
            candidate = await _fetch_chosen_candidate(session, payment_id, result["chosen_action"])
            await populate_ledger_and_audit_async(
                session,
                payment_id=payment_id,
                candidate_id=candidate["candidate_id"] if candidate else result["candidate_ids"][0],
                decision_id=result["decision_id"],
                verdict=result["verdict"],
                chosen_action=result["chosen_action"],
                recovery_prob_bps=candidate["recovery_prob_bps"] if candidate else 0,
                cost_paise=candidate["cost_paise"] if candidate else 0,
                actual_recovery_paise=0,
                diagnosis_id=diagnosis_id,
                outcome=None,
            )
    # else: ALLOW + an executing action -- workers/execution_worker.py owns
    # the terminal ledger/audit write once the job actually completes.


async def _ensure_consumer_group(redis: aioredis.Redis) -> None:
    try:
        await redis.xgroup_create(STREAM_NAME, GROUP_NAME, id="0", mkstream=True)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def _process_batch(redis: aioredis.Redis, messages: list[tuple[str, dict[str, str]]]) -> None:
    for stream_msg_id, raw_msg in messages:
        if raw_msg.get("event_type") != TRIGGERING_EVENT_TYPE:
            await redis.xack(STREAM_NAME, GROUP_NAME, stream_msg_id)
            continue
        try:
            await process_payment_failure(raw_msg["payment_id"], raw_msg.get("bank") or None, redis)
            await redis.xack(STREAM_NAME, GROUP_NAME, stream_msg_id)
        except Exception:
            logger.exception(
                "[Pipeline] failed processing payment_id=%s, leaving pending",
                raw_msg.get("payment_id"),
            )


async def _reclaim_pending(redis: aioredis.Redis) -> None:
    while True:
        next_id, messages, _ = await redis.xautoclaim(
            STREAM_NAME,
            GROUP_NAME,
            CONSUMER_NAME,
            min_idle_time=PENDING_RECLAIM_IDLE_MS,
            start_id="0-0",
            count=BATCH_SIZE,
        )
        if not messages:
            break
        await _process_batch(redis, messages)
        if next_id == "0-0":
            break


async def run_consumer(redis: aioredis.Redis, *, max_iterations: int | None = None) -> None:
    await _ensure_consumer_group(redis)
    await _reclaim_pending(redis)

    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            results = await redis.xreadgroup(
                groupname=GROUP_NAME,
                consumername=CONSUMER_NAME,
                streams={STREAM_NAME: ">"},
                count=BATCH_SIZE,
                block=BLOCK_MS,
            )
            if not results:
                continue
            for _stream_name, messages in results:
                await _process_batch(redis, messages)
        except Exception:
            logger.exception("[Pipeline] consumer loop error")


async def main() -> None:
    import logging as _logging

    from recoveryos.config import get_settings

    _logging.basicConfig(
        level=_logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = get_settings()
    redis_client = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    try:
        await run_consumer(redis_client)
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
