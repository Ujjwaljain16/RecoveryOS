"""Risk summary router — GET /v1/risk/summary"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from recoveryos.database import get_app_session

router = APIRouter()


@router.get("/summary", summary="Revenue-at-risk summary for a merchant")
async def risk_summary(
    merchant_id: str,
    session: AsyncSession = Depends(get_app_session),
):
    """
    Returns total revenue at risk, recoverable estimate, and affected payment count.
    Phase 3 implementation: query recovery_ledger aggregates.
    """
    return {
        "merchant_id": merchant_id,
        "total_revenue_at_risk_paise": 0,
        "recoverable_estimate_paise": 0,
        "affected_payment_count": 0,
    }
