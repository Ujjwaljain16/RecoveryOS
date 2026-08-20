"""Audit explorer router — GET /v1/audit/{payment_id}"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from recoveryos.database import get_app_session

router = APIRouter()


@router.get("/{payment_id}", summary="Full audit trail for a payment (replayable)")
async def audit_trail(
    payment_id: str,
    session: AsyncSession = Depends(get_app_session),
):
    """
    Returns the complete, immutable audit chain for a payment.
    Every step references an audit_log row — joins events, diagnoses,
    candidate_actions, policy_decisions, recoveries.
    Phase 9 implementation: full audit explorer with is_fallback flag visible.
    """
    return {"payment_id": payment_id, "audit_chain": []}
