"""
Simulation control router — POST /v1/simulate/degrade
ONLY enabled when ENV=demo (enforced at app factory level in main.py).
This is the live demo hook from PRD §38.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter()


class DegradeRequest(BaseModel):
    bank: str
    method: str
    target_success_rate: float = Field(ge=0.0, le=1.0)
    duration_minutes: int = Field(gt=0, le=480)


@router.post("/degrade", summary="[DEMO ONLY] Inject bank degradation scenario")
async def simulate_degrade(payload: DegradeRequest):
    """
    Triggers a synthetic bank degradation event.
    The Risk Engine will detect the anomaly within one 15-min bucket window,
    form a systemic cohort, suppress immediate retries, and reschedule.
    Phase 1 implementation: full simulator integration.
    """
    return {
        "status": "degradation_injected",
        "bank": payload.bank,
        "method": payload.method,
        "target_success_rate": payload.target_success_rate,
        "duration_minutes": payload.duration_minutes,
    }
