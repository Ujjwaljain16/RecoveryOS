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

    STATUS (re-checked in the pre-Phase-8 audit): recovery_ledger is no
    longer empty — services/pipeline/ledger.py has been writing real rows
    to it since Phase 7 (966/966 payments in the Phase 7 10k run), and
    services/risk_engine/anomaly.py (Phase 4) is a real, tested anomaly
    detector. This endpoint is just not wired to aggregate that data yet —
    deferred to Phase 9 (dashboard) so the response shape is designed
    against actual UI needs rather than guessed now and redone later.
    Still 501, but the REASON is "not wired," not "nothing to wire to."
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "Risk summary is not implemented yet. The underlying data now "
            "exists (recovery_ledger has been written to since Phase 7, "
            "and the anomaly detector has been real since Phase 4), but "
            "this endpoint is not yet wired to aggregate and serve it — "
            "real implementation deferred to Phase 9 (dashboard)."
        ),
    )
