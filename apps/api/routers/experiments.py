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

    STATUS (re-checked in the pre-Phase-8 audit): recovery_ledger and
    baseline_runs are no longer empty — services/pipeline/ledger.py and
    services/pipeline/baseline.py have been writing real rows to both
    since Phase 7 (proven end-to-end: 966/966 payments reached a real
    recovery_ledger row with a real baseline_outcome in the Phase 7 10k
    run). This endpoint is just not wired to query them yet — deferred to
    Phase 9 (dashboard) so the response shape is designed against actual
    UI needs rather than guessed now and redone later. Still 501, but the
    REASON is "not wired," not "nothing to wire to."
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "Experiment results are not implemented yet for the live API. "
            "The underlying data now exists (recovery_ledger and "
            "baseline_runs have been written to since Phase 7), but this "
            "endpoint is not yet wired to serve it — real implementation "
            "deferred to Phase 9 (dashboard)."
        ),
    )
