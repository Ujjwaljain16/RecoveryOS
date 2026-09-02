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

from recoveryos.metrics import stream_backlog_depth
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


async def process_payment_failure(
    payment_id: str, bank: str | None, redis: aioredis.Redis, source_event_id: str | None = None
) -> None:
    """
    One full decision chain for one failed payment. Raises on genuine
    infrastructure failure (DB unreachable, etc.) so the caller leaves the
    stream message pending for retry -- does NOT raise merely because the
    AI Diagnoser was unavailable, since diagnose_and_persist already
    resolves that internally via the deterministic fallback (Phase 4).

    source_event_id (Task S1, pre-Phase-8 audit): the triggering
    stream:risk_engine message's own source_event_id (see
    services/event_processor/publisher.py), threaded through to
    diagnose_and_persist/decide_and_persist so a message redelivered after
    this function already fully succeeded once (e.g. the xack call itself
    failing right after a successful run, per _process_batch below) writes
    into the SAME diagnosis/candidate_actions/policy_decision rows instead
    of creating duplicates -- see migrations/0013's UNIQUE constraints.

    Phase 12/13: this function drives a payment's RecoveryMission through
    OBSERVED/OBSERVING_OUTCOME -> INVESTIGATING -> PLANNING ->
    AWAITING_AUTHORIZATION -> {EXECUTING | ESCALATED | TERMINATED}. It
    doesn't need to know whether it's handling a brand-new PAYMENT_FAILED
    event or a Phase-13 replan fired by workers/retry_scheduler.py --
    get_or_create_mission_async's own lookup-by-payment_id finds the SAME
    active mission either way, and was_created alone decides whether to log
    MISSION_CREATED or REINVESTIGATION_STARTED. See services/recovery_engine/
    mission.py's module docstring for the full state-ownership discipline.
    """
    from recoveryos import clock
    from recoveryos.config import get_settings
    from recoveryos.database import get_app_session_factory
    from services.pipeline.baseline import compute_and_persist_baseline_run
    from services.recovery_engine.mission import (
        get_or_create_mission_async,
        transition_mission_async,
    )

    settings = get_settings()

    async with get_app_session_factory()() as session:
        await _run_anomaly_detection_for_payment(session, bank)
        # Computed here, unconditionally, BEFORE the ALLOW/DO_NOTHING branch
        # below -- whichever path ends up writing the terminal ledger row
        # (this consumer immediately, or execution_worker later) needs a
        # baseline_runs row to already exist; execution_worker's sync path
        # only READS baseline_runs, it never computes one.
        await compute_and_persist_baseline_run(session, payment_id)
        payment_row = (
            (
                await session.execute(
                    text("SELECT amount_paise FROM payments WHERE payment_id = :pid"),
                    {"pid": payment_id},
                )
            )
            .mappings()
            .first()
        )
    amount_paise = payment_row["amount_paise"] if payment_row else 0

    async with get_app_session_factory()() as session:
        mission, was_created = await get_or_create_mission_async(
            session,
            payment_id=payment_id,
            amount_paise=amount_paise,
            now=clock.utcnow(),
            max_investigation_rounds=settings.mission_max_investigation_rounds,
            max_attempts=settings.mission_max_attempts,
            max_mission_duration_seconds=settings.mission_max_duration_seconds,
        )
    mission_id = mission["mission_id"]
    # A redelivery of the SAME triggering event can land here mid-flight --
    # e.g. the xack call failing right after a fully successful prior run
    # (same class of redelivery migrations/0013's UNIQUE constraints
    # already guard diagnosis/decision rows against). If the mission's
    # recorded state isn't one that legitimately precedes INVESTIGATING,
    # this call is that kind of redelivery landing on an already-in-flight
    # (or already-terminal, racing a fresh get_or_create) mission -- skip
    # ALL mission-tracking calls for this invocation rather than let
    # transition_mission_async raise on an illegal transition and strand
    # the message in an infinite redelivery loop. The rest of the pipeline
    # (diagnose/decide/execute) still runs and dedupes correctly on its own
    # existing constraints regardless -- this only affects the mission
    # audit trail's completeness for the rare redelivery case, never
    # correctness or safety.
    mission_trackable = mission["state"] in ("OBSERVED", "OBSERVING_OUTCOME")

    if mission_trackable:
        async with get_app_session_factory()() as session:
            if was_created:
                await transition_mission_async(
                    session,
                    mission_id=mission_id,
                    to_state="INVESTIGATING",
                    event_type="MISSION_CREATED",
                    actor="system",
                    payload={"payment_id": payment_id, "source_event_id": source_event_id},
                    now=clock.utcnow(),
                )
            else:
                await transition_mission_async(
                    session,
                    mission_id=mission_id,
                    to_state="INVESTIGATING",
                    event_type="REINVESTIGATION_STARTED",
                    actor="system",
                    payload={"source_event_id": source_event_id},
                    increment_round=True,
                    now=clock.utcnow(),
                )

    # diagnose_and_persist opens its own diagnoser_role + app_role sessions
    # internally -- never raises merely because the LLM is unreachable/
    # times out (that's exactly what the fallback path is for).
    diagnosis = await diagnose_and_persist(payment_id, source_event_id)
    diagnosis_id = diagnosis.diagnosis_id if diagnosis is not None else None

    if mission_trackable:
        async with get_app_session_factory()() as session:
            await _log_investigation_events(
                session, mission_id=mission_id, diagnosis_id=diagnosis_id
            )
            await transition_mission_async(
                session,
                mission_id=mission_id,
                to_state="PLANNING",
                event_type="INVESTIGATION_CONCLUDED",
                actor="system",
                payload={"diagnosis_id": diagnosis_id},
                now=clock.utcnow(),
            )
            await transition_mission_async(
                session,
                mission_id=mission_id,
                to_state="AWAITING_AUTHORIZATION",
                event_type="PLANNING_CONCLUDED",
                actor="system",
                payload={},
                now=clock.utcnow(),
            )

    async def _authorize_execution_before_enqueue(enqueue_ctx: dict) -> None:
        # Runs INSIDE decide_and_persist, before the job it just built is
        # enqueued -- committing this transition here (not after
        # decide_and_persist returns, as it used to) closes a real race
        # against workers/execution_worker.py's own near-instant pickup of
        # that job. See decide_and_persist's own docstring for the full
        # story; this is the ONLY case reaching that hook (RETRY_LATER
        # returns from decide_and_persist before ever calling it; a
        # verdict!=ALLOW/DO_NOTHING decision never enqueues at all), so
        # to_state/event_type here are always EXECUTING/POLICY_AUTHORIZED.
        async with get_app_session_factory()() as hook_session:
            await transition_mission_async(
                hook_session,
                mission_id=mission_id,
                to_state="EXECUTING",
                event_type="POLICY_AUTHORIZED",
                actor="policy_engine",
                payload={
                    "decision_id": enqueue_ctx["decision_id"],
                    "verdict": "ALLOW",
                    "chosen_action": enqueue_ctx["chosen_action"],
                    "blocking_rule": None,
                },
                now=clock.utcnow(),
            )

    result = await decide_and_persist(
        payment_id,
        redis_client=redis,
        source_event_id=source_event_id,
        diagnosis_id=diagnosis_id,
        before_enqueue=_authorize_execution_before_enqueue if mission_trackable else None,
    )

    policy_payload = {
        "decision_id": result["decision_id"],
        "verdict": result["verdict"],
        "chosen_action": result["chosen_action"],
        "blocking_rule": result.get("blocking_rule"),
    }
    # already_authorized: the EXECUTING/POLICY_AUTHORIZED transition below
    # was already committed by _authorize_execution_before_enqueue, inside
    # decide_and_persist, before it enqueued the job -- true for every ALLOW
    # decision that actually enqueues (RETRY_NOW/ALT_ROUTE/REMINDER/ESCALATE).
    # RETRY_LATER never reaches the enqueue branch (decide_and_persist
    # returns earlier for it), so its own EXECUTING->OBSERVING_OUTCOME pair
    # below still needs to run here, same as before this fix.
    already_authorized = False
    if result["verdict"] != "ALLOW":
        to_state = "ESCALATED" if result["verdict"] == "ESCALATE" else "TERMINATED"
        event_type = "POLICY_ESCALATED" if result["verdict"] == "ESCALATE" else "POLICY_BLOCKED"
    elif result["chosen_action"] == "DO_NOTHING":
        to_state, event_type = "TERMINATED", "POLICY_DO_NOTHING"
    else:
        to_state, event_type = "EXECUTING", "POLICY_AUTHORIZED"
        already_authorized = mission_trackable and result["chosen_action"] != "RETRY_LATER"

    if mission_trackable and not already_authorized:
        async with get_app_session_factory()() as session:
            await transition_mission_async(
                session,
                mission_id=mission_id,
                to_state=to_state,
                event_type=event_type,
                actor="policy_engine",
                payload=policy_payload,
                now=clock.utcnow(),
            )
            if result["verdict"] == "ALLOW" and result["chosen_action"] == "RETRY_LATER":
                # "Executing" a deferred wait completes instantly -- the actual
                # observation period is the wait itself, resolved later by
                # workers/retry_scheduler.py firing (Phase 13's shared
                # OBSERVING_OUTCOME -> INVESTIGATING loop, the same transition
                # a FAILED immediate attempt's reschedule also resolves through).
                await transition_mission_async(
                    session,
                    mission_id=mission_id,
                    to_state="OBSERVING_OUTCOME",
                    event_type="RETRY_LATER_SCHEDULED",
                    actor="system",
                    payload={
                        "scheduled_reevaluation_id": result.get("scheduled_reevaluation_id"),
                        "scheduled_for": result.get("scheduled_for"),
                    },
                    now=clock.utcnow(),
                )

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


async def _log_investigation_events(
    session: AsyncSession, *, mission_id: str, diagnosis_id: str | None
) -> None:
    """Phase 12 -- HYPOTHESIS_UPDATED (always, when a diagnosis exists) and
    AI_RECOMMENDATION (only when the investigator actually ran and produced
    one, Phase 11). Narration only -- logged via log_mission_event_async,
    which does NOT change recovery_missions.state; the mission is still
    INVESTIGATING at this point."""
    from services.recovery_engine.mission import log_mission_event_async

    if diagnosis_id is None:
        return

    diag_row = (
        (
            await session.execute(
                text(
                    "SELECT root_cause, confidence, confidence_band, is_fallback "
                    "FROM diagnoses WHERE diagnosis_id = :did"
                ),
                {"did": diagnosis_id},
            )
        )
        .mappings()
        .first()
    )
    if diag_row is not None:
        await log_mission_event_async(
            session,
            mission_id=mission_id,
            event_type="HYPOTHESIS_UPDATED",
            actor="ai",
            payload={
                "diagnosis_id": diagnosis_id,
                "root_cause": diag_row["root_cause"],
                "confidence": (
                    float(diag_row["confidence"]) if diag_row["confidence"] is not None else None
                ),
                "confidence_band": diag_row["confidence_band"],
                "is_fallback": diag_row["is_fallback"],
            },
        )

    rec_row = (
        (
            await session.execute(
                text(
                    "SELECT recommended_action, confidence, risk_flags, recovery_rationale "
                    "FROM recovery_recommendations WHERE diagnosis_id = :did "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"did": diagnosis_id},
            )
        )
        .mappings()
        .first()
    )
    if rec_row is not None:
        await log_mission_event_async(
            session,
            mission_id=mission_id,
            event_type="AI_RECOMMENDATION",
            actor="ai",
            payload={
                "recommended_action": rec_row["recommended_action"],
                "confidence": float(rec_row["confidence"]),
                "risk_flags": rec_row["risk_flags"],
                "recovery_rationale": rec_row["recovery_rationale"],
            },
        )


async def _record_backlog(redis: aioredis.Redis) -> None:
    """Domain Audit finding #4 -- same reasoning as
    services/event_processor/consumer.py's own _record_backlog, for the
    second hop in the pipeline (stream:risk_engine). Best-effort."""
    try:
        groups = await redis.xinfo_groups(STREAM_NAME)
        for group in groups:
            if group.get("name") == GROUP_NAME:
                lag = group.get("lag")
                if lag is not None:
                    stream_backlog_depth.labels(stream=STREAM_NAME, group=GROUP_NAME).set(lag)
                break
    except Exception:
        logger.exception("[Pipeline] failed to record stream backlog (non-fatal)")


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
            await process_payment_failure(
                raw_msg["payment_id"],
                raw_msg.get("bank") or None,
                redis,
                source_event_id=raw_msg.get("source_event_id") or None,
            )
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
            await _record_backlog(redis)
            if not results:
                continue
            for _stream_name, messages in results:
                await _process_batch(redis, messages)
        except Exception:
            logger.exception("[Pipeline] consumer loop error")


async def main() -> None:
    import logging as _logging

    from prometheus_client import start_http_server

    from recoveryos.config import get_settings

    _logging.basicConfig(
        level=_logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = get_settings()
    # TRD §10: this process has no HTTP server of its own (a bare Redis
    # consumer loop) -- most of the §10 series are actually recorded HERE
    # (diagnosis_latency_seconds, ai_diagnoser_fallback_total via
    # diagnose_and_persist; revenue_*_paise_total via ledger.py for the
    # no-execution BLOCK/DO_NOTHING path; policy_blocks_total via
    # decide_and_persist), so it needs its own scrape port, not just api's.
    start_http_server(settings.prometheus_port)
    redis_client = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    try:
        await run_consumer(redis_client)
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
