"""
Retry Scheduler — Task REPLAN1, the continuous-replanning half of the
RETRY_LATER path.

Async, like services/pipeline/consumer.py, NOT sync like
workers/execution_worker.py: this worker re-runs diagnose_and_persist /
decide_and_persist, both of which are already async (asyncpg + httpx to
the LLM), so there is no reason to force a sync/async bridge here the way
execution_worker deliberately avoids one for its own (unrelated) reasons.

Polls scheduled_reevaluations for rows whose time has come (a fresh PENDING
row, OR a FIRED row whose lease has expired -- adversarial sweep finding
#50's reclaim path, services/recovery_engine/scheduling.py), atomically
claims each one (claim_reevaluation's WHERE clause is the whole concurrency
mechanism, no advisory lock needed), then re-runs
services.pipeline.consumer.process_payment_failure for that payment with a
FRESH source_event_id (fired_source_event_id) -- a genuine re-evaluation
against current anomaly/cooldown/attempt-number state, not a replay of the
stale decision that produced this row. On success the row is marked
COMPLETED; if a claim is reclaimed (its original owner crashed), the
mission's CURRENT state is checked first -- still OBSERVING_OUTCOME means
genuinely safe to redo, anything else means the crash happened after real
progress was already made durably through some other path, and the row is
marked CANCELLED instead of being reprocessed (see cancel_stale_reevaluation's
docstring for why this is what actually makes reclaiming safe).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import text

from recoveryos import clock
from recoveryos.database import get_app_session_factory
from services.pipeline.consumer import process_payment_failure
from services.recovery_engine.scheduling import (
    cancel_stale_reevaluation,
    claim_reevaluation,
    complete_reevaluation,
    fetch_due_reevaluations,
)

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


async def _mission_still_observing_outcome(mission_id: str | None) -> bool:
    """
    True if mission_id is None (nothing to gate against -- a row written
    before Recovery Mission tracking existed, or by a caller that doesn't
    track missions; same permissive default services/pipeline/consumer.py's
    own mission_trackable guard uses) OR the mission's REAL current state is
    still OBSERVING_OUTCOME. False means some other path already advanced
    (or terminated) this mission since the row was scheduled -- reprocessing
    would attempt an OBSERVING_OUTCOME -> INVESTIGATING transition against a
    mission that has already left OBSERVING_OUTCOME, a duplicate-mission-
    event hazard, not a safe redo.
    """
    if mission_id is None:
        return True
    async with get_app_session_factory()() as session:
        row = (
            await session.execute(
                text("SELECT state FROM recovery_missions WHERE mission_id = :mid"),
                {"mid": mission_id},
            )
        ).first()
    return row is not None and row[0] == "OBSERVING_OUTCOME"


async def _process_one(row: dict, redis_client) -> None:
    # fetch_due_reevaluations reads via asyncpg (.mappings()), which returns
    # a native uuid.UUID for a UUID column, not the plain str
    # process_payment_failure/enqueue_recovery_job expect end to end --
    # str() here, once, matches every other UUID-bearing row this codebase
    # threads through Redis (e.g. services/pipeline/consumer.py's own
    # payment_id is always already a str by the time it reaches here).
    # Surfaced by the closed-loop replan work: a fired re-evaluation that
    # now genuinely re-decides an EXECUTING action (RETRY_NOW/ALT_ROUTE) reaches
    # enqueue_recovery_job's redis.xadd() call, which -- unlike SQL/asyncpg --
    # rejects a raw UUID object outright; the pre-existing RETRY_LATER-only
    # path never actually enqueued a job, so this was never exercised.
    payment_id = str(row["payment_id"])
    reevaluation_id = row["reevaluation_id"]
    fired_source_event_id = str(uuid.uuid4())
    async with get_app_session_factory()() as session:
        won = await claim_reevaluation(
            session, reevaluation_id, fired_source_event_id, clock.utcnow()
        )
    if not won:
        # Another scheduler instance (or another poll cycle) claimed it
        # first -- not an error.
        return

    # Re-Audit finding (MEDIUM): the "still OBSERVING_OUTCOME?" check below
    # and the process_payment_failure re-investigation after it used to run
    # unlocked -- a webhook landing on services/pipeline/reconciliation.py
    # in between (closing this same mission out to RECOVERED/TERMINATED)
    # would leave this scheduler acting on a stale read, spawning a
    # duplicate investigation and burning a real LLM call for a mission
    # that's already resolved. Backstopped elsewhere from actually double-
    # charging, but wasteful and noisy -- see services/pipeline/
    # reconciliation.py's own matching lock (same key format, `mission:
    # {payment_id}`) for the other side of this race.
    from recoveryos.database import advisory_lock_async

    async with (
        get_app_session_factory()() as session,
        advisory_lock_async(session, key=f"mission:{payment_id}"),
    ):
        if not await _mission_still_observing_outcome(row.get("mission_id")):
            logger.info(
                "[RetryScheduler] reevaluation_id=%s reclaimed but its mission has already "
                "moved past OBSERVING_OUTCOME -- marking CANCELLED instead of reprocessing "
                "(stale, not an error)",
                reevaluation_id,
            )
            await cancel_stale_reevaluation(session, reevaluation_id)
            return

        bank = await _fetch_bank(payment_id)
        await process_payment_failure(
            payment_id, bank, redis_client, source_event_id=fired_source_event_id
        )
        await complete_reevaluation(session, reevaluation_id)


async def _sweep_expired_missions() -> int:
    """
    Re-Audit finding (HIGH): recovery_missions.expires_at (and the
    max_mission_duration_seconds it's computed from) is set at mission
    creation, persisted, and displayed via the API/dashboard -- but nothing
    EVER enforced it proactively. services/recovery_engine/mission.py's
    check_budget() DOES check it, but only reactively, as a gate before
    scheduling the NEXT round -- a mission sitting in OBSERVING_OUTCOME on a
    real PENDING recovery whose webhook never arrives (a dead ngrok tunnel,
    a customer who never completes checkout, a lost Razorpay delivery) never
    reaches ANY code path that calls check_budget() at all, so it sits past
    its own expires_at forever, contradicting mission.py's own docstring
    claim that the budget fields are enforced.

    Runs once per poll cycle (same cadence as the reevaluation poll above,
    reusing this already-running worker rather than standing up a new one)
    -- finds every eligible non-terminal mission whose expires_at has passed
    and terminates it for real, with its own distinct event type so a
    dashboard/audit reader can tell "ran out of budget while waiting" apart
    from every other TERMINATED reason. Two states are deliberately excluded
    -- not swept, left for their own existing mechanism instead of forcing
    an ALLOWED_TRANSITIONS path open just for this:
      - EXECUTING: mid-execution, covered by execution_worker.py's own
        crash-recovery/redelivery path, a different, already-tested
        mechanism, not a duration budget concern.
      - OBSERVED: mission.py's own ALLOWED_TRANSITIONS only permits
        OBSERVED -> INVESTIGATING, never OBSERVED -> TERMINATED directly (a
        real constraint, found live via this function's own test suite, not
        an oversight to work around) -- and in practice this state is held
        for a single synchronous step immediately after mission creation
        (get_or_create_mission_async's own caller transitions it to
        INVESTIGATING in the same call), so a mission genuinely stuck here
        for a FULL mission-duration budget would mean the process crashed
        between two statements of one function -- an already-covered crash-
        recovery scenario (a payment with no active mission at all gets a
        fresh one created on its next real ingestion), not a distinct gap
        worth bending the state machine's own discipline for.
    """
    from services.recovery_engine.mission import (
        InvalidMissionTransitionError,
        transition_mission_async,
    )

    now = clock.utcnow()
    async with get_app_session_factory()() as session:
        expired = (
            await session.execute(
                text(
                    "SELECT mission_id FROM recovery_missions "
                    "WHERE state NOT IN "
                    "('RECOVERED','ESCALATED','TERMINATED','EXECUTING','OBSERVED') "
                    "AND expires_at < :now"
                ),
                {"now": now},
            )
        ).all()

        swept = 0
        for (mission_id,) in expired:
            try:
                await transition_mission_async(
                    session,
                    mission_id=mission_id,
                    to_state="TERMINATED",
                    event_type="MISSION_BUDGET_EXHAUSTED",
                    actor="system",
                    payload={"reason": "MISSION_DURATION_EXCEEDED", "source": "expiry_sweep"},
                    now=now,
                )
                swept += 1
            except InvalidMissionTransitionError:
                # Benign race: some other path (a webhook, a reevaluation)
                # already moved this mission on between the SELECT above and
                # this transition attempt -- not a sweep failure.
                logger.info(
                    "[RetryScheduler] mission_id=%s no longer eligible for expiry sweep by the "
                    "time this transition ran -- already advanced elsewhere",
                    mission_id,
                )
    return swept


async def run_once(redis_client) -> int:
    """One poll cycle. Returns how many due REEVALUATION rows were processed
    (test hook, pre-existing contract -- unchanged). The expired-mission
    sweep below runs every cycle too but reports its own count only via
    logging, not this return value, to avoid disturbing that contract."""
    async with get_app_session_factory()() as session:
        due = await fetch_due_reevaluations(session, clock.utcnow())

    for row in due:
        try:
            await _process_one(row, redis_client)
        except Exception:
            logger.exception(
                "[RetryScheduler] failed re-evaluating reevaluation_id=%s (row stays FIRED; "
                "its lease will expire in REEVALUATION_LEASE_SECONDS and it will be reclaimed "
                "and retried automatically on a future poll cycle -- adversarial sweep finding #50)",
                row["reevaluation_id"],
            )

    try:
        swept = await _sweep_expired_missions()
        if swept:
            logger.info("[RetryScheduler] expiry sweep terminated %s mission(s)", swept)
    except Exception:
        logger.exception("[RetryScheduler] expiry sweep failed (non-fatal, retried next cycle)")

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
