"""Payment detail router — GET /v1/payments/{payment_id}/detail"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from recoveryos.database import get_app_session

router = APIRouter()


@router.get("/{payment_id}/detail", summary="Full decision chain for a payment")
async def payment_detail(
    payment_id: str,
    session: AsyncSession = Depends(get_app_session),
):
    """
    Returns payment, diagnosis, candidate actions, policy decision, and recovery history.
    Phase 5+ implementation: full join across all tables.
    """
    return {
        "payment_id": payment_id,
        "payment": None,
        "diagnosis": None,
        "candidate_actions": [],
        "policy_decision": None,
        "recovery_history": [],
    }
