"""Risk summary router — GET /v1/risk/summary"""

from fastapi import APIRouter, Depends, HTTPException, status

from apps.api.dependencies.auth import verify_api_key
from recoveryos.models import Merchant

router = APIRouter()


@router.get("/summary", summary="Revenue-at-risk summary for the authenticated merchant")
async def risk_summary(merchant: Merchant = Depends(verify_api_key)):
    """
    Task 6: this used to return a confident-looking
    {"total_revenue_at_risk_paise": 0, "recoverable_estimate_paise": 0, ...}
    unconditionally — indistinguishable, to any caller integrating against
    it, from "this merchant genuinely has zero revenue at risk." It took a
    live DB session as a dependency and never used it.

    501s explicitly instead of faking a zero: this depends on
    recovery_ledger aggregates, and nothing writes to recovery_ledger yet —
    risk_engine and recovery_engine are both still empty packages (see the
    architecture recon). A caller getting a clear "not implemented" can
    build around that; a caller getting a confident zero cannot tell the
    difference from real data and may build on a lie.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "Risk summary is not implemented yet. It depends on recovery_ledger "
            "aggregates, and nothing currently writes to recovery_ledger — the "
            "risk and recovery engines don't exist yet. This is an honest 501, "
            "not a placeholder zero."
        ),
    )
