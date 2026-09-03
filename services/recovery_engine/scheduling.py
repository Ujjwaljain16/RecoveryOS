"""
Continuous replanning — Task REPLAN1, generalized beyond RETRY_LATER to
the full closed loop. Writes and claims scheduled_reevaluations rows
(migration 0017, mission_id column added by migration 0022). app_role
only, same as every other write in services/recovery_engine/services.pipeline.

Two writers now, same async/sync split as services/pipeline/ledger.py:
  - schedule_reevaluation (async): services/recovery_engine/orchestrator.py's
    existing RETRY_LATER path.
  - schedule_reevaluation_sync (newer): workers/execution_worker.py's
    sync path, when a FAILED RETRY_NOW/ALT_ROUTE attempt still has mission
    budget remaining -- the closed loop this system was missing (see
    services/recovery_engine/mission.py's module docstring).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from recoveryos.database import get_app_session_factory

# Adversarial sweep finding #50 -- how long a claim (status='FIRED') is
# valid before it becomes reclaimable. Must comfortably exceed any
# legitimate _process_one() duration (diagnosis: up to
# MAX_INVESTIGATION_ROUNDS=2 real LLM calls + 1 finalize call, each bounded
# by AI_DIAGNOSER_TIMEOUT_SECONDS, plus DB writes) so a slow-but-healthy
# claim is never falsely reclaimed out from under itself; short enough that
# a genuine crash recovers in bounded time rather than staying orphaned
# indefinitely (the previous behavior). Not a Settings field -- this is an
# evaluation-agnostic reliability constant, not something a deployment
# should need to tune.
REEVALUATION_LEASE_SECONDS = 120


async def schedule_reevaluation(
    *,
    payment_id: str,
    decision_id: str,
    diagnosis_id: str | None,
    source_event_id: str | None,
    scheduled_for: datetime,
    mission_id: str | None = None,
) -> str:
    """
    Insert a PENDING row. Same S1 dedup discipline as diagnoses/
    candidate_actions/policy_decisions (migration 0013): a redelivered
    message for the SAME triggering event returns the existing row instead
    of scheduling a second re-evaluation. Returns the reevaluation_id
    (either newly inserted or the pre-existing one).

    mission_id (optional): which mission this re-evaluation
    belongs to -- purely for audit/query correlation (workers/
    retry_scheduler.py finds the SAME active mission by payment_id
    regardless, via services.recovery_engine.mission.get_or_create_mission_async's
    own lookup, so this is not on the critical path for correctness).
    """
    async with get_app_session_factory()() as session:
        reevaluation_id = str(uuid.uuid4())
        result = await session.execute(
            text(
                "INSERT INTO scheduled_reevaluations "
                "(reevaluation_id, payment_id, decision_id, diagnosis_id, source_event_id, "
                "scheduled_for, status, mission_id) "
                "VALUES (:rid, :pid, :did, :diag_id, :sid, :sched, 'PENDING', :mission_id) "
                "ON CONFLICT (payment_id, source_event_id) DO NOTHING "
                "RETURNING reevaluation_id"
            ),
            {
                "rid": reevaluation_id,
                "pid": payment_id,
                "did": decision_id,
                "diag_id": diagnosis_id,
                "sid": source_event_id,
                "sched": scheduled_for,
                "mission_id": mission_id,
            },
        )
        row = result.first()
        if row is None:
            existing = (
                await session.execute(
                    text(
                        "SELECT reevaluation_id FROM scheduled_reevaluations "
                        "WHERE payment_id = :pid AND source_event_id = :sid"
                    ),
                    {"pid": payment_id, "sid": source_event_id},
                )
            ).first()
            await session.commit()
            return existing[0]
        await session.commit()
        return row[0]


def schedule_reevaluation_sync(
    conn,
    *,
    payment_id: str,
    decision_id: str,
    diagnosis_id: str | None,
    source_event_id: str | None,
    scheduled_for: datetime,
    mission_id: str | None = None,
) -> str:
    """Sync mirror of schedule_reevaluation, for workers/execution_worker.py's
    closed-loop-on-FAILED path. Same dedup/ON CONFLICT discipline."""
    reevaluation_id = str(uuid.uuid4())
    result = conn.execute(
        text(
            "INSERT INTO scheduled_reevaluations "
            "(reevaluation_id, payment_id, decision_id, diagnosis_id, source_event_id, "
            "scheduled_for, status, mission_id) "
            "VALUES (:rid, :pid, :did, :diag_id, :sid, :sched, 'PENDING', :mission_id) "
            "ON CONFLICT (payment_id, source_event_id) DO NOTHING "
            "RETURNING reevaluation_id"
        ),
        {
            "rid": reevaluation_id,
            "pid": payment_id,
            "did": decision_id,
            "diag_id": diagnosis_id,
            "sid": source_event_id,
            "sched": scheduled_for,
            "mission_id": mission_id,
        },
    )
    row = result.first()
    if row is None:
        existing = conn.execute(
            text(
                "SELECT reevaluation_id FROM scheduled_reevaluations "
                "WHERE payment_id = :pid AND source_event_id = :sid"
            ),
            {"pid": payment_id, "sid": source_event_id},
        ).first()
        conn.commit()
        return existing[0]
    conn.commit()
    return row[0]


async def fetch_due_reevaluations(
    session: AsyncSession, now: datetime, limit: int = 50
) -> list[dict]:
    """
    Rows whose scheduled_for has passed and are still PENDING, PLUS rows
    that were claimed (FIRED) but whose lease has since expired -- the
    latter is adversarial sweep finding #50's reclaim path: a row a
    previous claimant crashed on before completing is exactly as "due" as
    one that was never claimed at all. Read-only — claiming happens
    per-row in claim_reevaluation() so a batch fetch here never races with
    the actual claim.
    """
    rows = (
        (
            await session.execute(
                text(
                    "SELECT reevaluation_id, payment_id, decision_id, diagnosis_id, "
                    "source_event_id, mission_id "
                    "FROM scheduled_reevaluations "
                    "WHERE (status = 'PENDING' AND scheduled_for <= :now) "
                    "   OR (status = 'FIRED' AND lease_expires_at < :now) "
                    "ORDER BY scheduled_for ASC LIMIT :limit"
                ),
                {"now": now, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def claim_reevaluation(
    session: AsyncSession, reevaluation_id: str, fired_source_event_id: str, now: datetime
) -> bool:
    """
    Atomically claims one row -- either a fresh PENDING row or a
    FIRED-but-lease-expired row (a reclaim). Returns True iff THIS call won
    the claim. The WHERE clause is the entire concurrency-safety mechanism,
    covering both cases with one UPDATE: two callers racing on the same row
    (whether both see it as freshly PENDING, or one is reclaiming while
    another's lease is just now expiring) will both issue this UPDATE, but
    only one's WHERE clause still matches by the time it runs; the loser's
    UPDATE affects 0 rows. A successful claim (re)sets lease_expires_at --
    complete_reevaluation() clears the lease's relevance entirely by moving
    the row to a terminal status.
    """
    lease_expires_at = now + timedelta(seconds=REEVALUATION_LEASE_SECONDS)
    result = await session.execute(
        text(
            "UPDATE scheduled_reevaluations SET status = 'FIRED', claimed_at = :now, "
            "lease_expires_at = :lease, fired_source_event_id = :fired_sid "
            "WHERE reevaluation_id = :rid "
            "AND (status = 'PENDING' OR (status = 'FIRED' AND lease_expires_at < :now))"
        ),
        {
            "now": now,
            "lease": lease_expires_at,
            "fired_sid": fired_source_event_id,
            "rid": reevaluation_id,
        },
    )
    await session.commit()
    return result.rowcount == 1


async def complete_reevaluation(session: AsyncSession, reevaluation_id: str) -> None:
    """
    FIRED -> COMPLETED: this claim's work genuinely finished
    (process_payment_failure returned without raising). Scoped to
    status='FIRED' so a call racing against a lease that already expired
    and got reclaimed by someone else is a harmless no-op, not a stomp on
    the new claimant's row.
    """
    await session.execute(
        text(
            "UPDATE scheduled_reevaluations SET status = 'COMPLETED' "
            "WHERE reevaluation_id = :rid AND status = 'FIRED'"
        ),
        {"rid": reevaluation_id},
    )
    await session.commit()


async def cancel_stale_reevaluation(session: AsyncSession, reevaluation_id: str) -> None:
    """
    FIRED -> CANCELLED: this is a reclaimed row whose mission has already
    moved past OBSERVING_OUTCOME by some other path (the original claimant
    crashed AFTER durably making real progress, not before) -- reprocessing
    it would attempt an OBSERVING_OUTCOME -> INVESTIGATING transition
    against a mission that has already left OBSERVING_OUTCOME, which is
    exactly the "duplicate mission event" hazard a naive reclaim-and-redo
    fix would risk. Marking CANCELLED instead of reprocessing is what makes
    the reclaim mechanism safe: only rows whose mission is CONFIRMED still
    waiting ever reach process_payment_failure again.
    """
    await session.execute(
        text(
            "UPDATE scheduled_reevaluations SET status = 'CANCELLED' "
            "WHERE reevaluation_id = :rid AND status = 'FIRED'"
        ),
        {"rid": reevaluation_id},
    )
    await session.commit()
