"""Experiments router — GET /v1/experiments/{run_id}"""

from fastapi import APIRouter, Depends, HTTPException, status

from apps.api.dependencies.auth import verify_api_key
from recoveryos.models import Merchant

router = APIRouter()


@router.get("/{run_id}", summary="Evaluation: RecoveryOS vs baseline comparison")
async def experiment_results(run_id: str, merchant: Merchant = Depends(verify_api_key)):
    """
    Task 6: this used to return a fully-formed-looking comparison —
    {"baseline": {...0...}, "recoveryos": {...0...}, "incremental_recovery_paise": 0}
    — for ANY run_id, indistinguishable from a real experiment that
    genuinely recovered nothing.

    501s explicitly instead: this depends on recovery_ledger and
    baseline_runs, neither of which the live system writes to yet (the
    offline ML evaluation harness under models/recovery/ is real and
    produces real numbers — see phase_2_certificate.json — but it's a
    separate, disconnected pipeline, not this live API's data source).
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "Experiment results are not implemented yet for the live API. "
            "This depends on recovery_ledger + baseline_runs, which nothing "
            "in the live system writes to yet — the offline ML evaluation "
            "harness (models/recovery/) is real but is a separate pipeline. "
            "This is an honest 501, not a placeholder comparison."
        ),
    )
