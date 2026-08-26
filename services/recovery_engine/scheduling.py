"""
Continuous replanning — Task REPLAN1. Writes and claims
scheduled_reevaluations rows (migration 0017). app_role only, same as
every other write in services/recovery_engine/services.pipeline.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from recoveryos.database import get_app_session_factory


async def schedule_reevaluation(
    *,
    payment_id: str,
    decision_id: str,
    diagnosis_id: str | None,
    source_event_id: str | None,
    scheduled_for: datetime,
) -> str:
    """
    Insert a PENDING row. Same S1 dedup discipline as diagnoses/
    candidate_actions/policy_decisions (migration 0013): a redelivered
    message for the SAME triggering event returns the existing row instead
    of scheduling a second re-evaluation. Returns the reevaluation_id
    (either newly inserted or the pre-existing one).
    """
    async with get_app_session_factory()() as session:
        reevaluation_id = str(uuid.uuid4())
        result = await session.execute(
            text(
                "INSERT INTO scheduled_reevaluations "
                "(reevaluation_id, payment_id, decision_id, diagnosis_id, source_event_id, "
                "scheduled_for, status) "
                "VALUES (:rid, :pid, :did, :diag_id, :sid, :sched, 'PENDING') "
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


async def fetch_due_reevaluations(session: AsyncSession, now: datetime, limit: int = 50) -> list[dict]:
    """Rows whose scheduled_for has passed and are still PENDING. Read-only
    — claiming happens per-row in claim_reevaluation() so a batch fetch
    here never races with the actual claim."""
    rows = (
        await session.execute(
            text(
                "SELECT reevaluation_id, payment_id, decision_id, diagnosis_id, source_event_id "
                "FROM scheduled_reevaluations "
                "WHERE status = 'PENDING' AND scheduled_for <= :now "
                "ORDER BY scheduled_for ASC LIMIT :limit"
            ),
            {"now": now, "limit": limit},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def claim_reevaluation(
    session: AsyncSession, reevaluation_id: str, fired_source_event_id: str, now: datetime
) -> bool:
    """
    Atomically claims one row (PENDING -> FIRED). Returns True iff THIS
    call won the claim. The WHERE status='PENDING' clause is the entire
    concurrency-safety mechanism -- two scheduler processes racing on the
    same row will both issue this UPDATE, but only one's WHERE clause
    still matches by the time it runs; the loser's UPDATE affects 0 rows.
    """
    result = await session.execute(
        text(
            "UPDATE scheduled_reevaluations SET status = 'FIRED', claimed_at = :now, "
            "fired_source_event_id = :fired_sid "
            "WHERE reevaluation_id = :rid AND status = 'PENDING'"
        ),
        {"now": now, "fired_sid": fired_source_event_id, "rid": reevaluation_id},
    )
    await session.commit()
    return result.rowcount == 1
