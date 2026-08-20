"""Experiments router — GET /v1/experiments/{run_id}"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from recoveryos.database import get_app_session

router = APIRouter()


@router.get("/{run_id}", summary="Evaluation: RecoveryOS vs baseline comparison")
async def experiment_results(
    run_id: str,
    session: AsyncSession = Depends(get_app_session),
):
    """
    Returns incremental_recovery_paise = RecoveryOS total - baseline total.
    Phase 8 implementation: raw SQL over recovery_ledger + baseline_runs.
    """
    return {
        "run_id": run_id,
        "baseline": {"recovered_paise": 0, "payment_count": 0},
        "recoveryos": {"recovered_paise": 0, "payment_count": 0},
        "incremental_recovery_paise": 0,
        "chart_data": [],
    }
