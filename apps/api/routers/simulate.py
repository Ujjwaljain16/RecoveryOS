"""
Simulation control router — POST /v1/simulate/degrade
ONLY enabled when ENV=demo (enforced at app factory level in main.py).
This is the live demo hook from PRD §38.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from apps.api.dependencies.auth import verify_api_key
from recoveryos.models import Merchant

router = APIRouter()


class DegradeRequest(BaseModel):
    bank: str
    method: str
    target_success_rate: float = Field(ge=0.0, le=1.0)
    duration_minutes: int = Field(gt=0, le=480)


@router.post("/degrade", summary="[DEMO ONLY] Inject bank degradation scenario")
async def simulate_degrade(
    payload: DegradeRequest,
    merchant: Merchant = Depends(verify_api_key),
):
    """
    Triggers a synthetic bank degradation event.
    The Risk Engine will detect the anomaly within one 15-min bucket window,
    form a systemic cohort, suppress immediate retries, and reschedule.

    Task 4: requires a valid API key like every other route now — a demo
    trigger is still a mutating, environment-wide action and shouldn't be
    the one unauthenticated door left open.

    Phase 1 implementation: full simulator integration.
    """
    return {
        "status": "degradation_injected",
        "bank": payload.bank,
        "method": payload.method,
        "target_success_rate": payload.target_success_rate,
        "duration_minutes": payload.duration_minutes,
    }
