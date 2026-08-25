"""Audit explorer router — GET /v1/audit/{payment_id}"""

from fastapi import APIRouter, Depends, HTTPException, status

from apps.api.dependencies.auth import verify_api_key
from recoveryos.models import Merchant

router = APIRouter()


@router.get("/{payment_id}", summary="Full audit trail for a payment (replayable)")
async def audit_trail(payment_id: str, merchant: Merchant = Depends(verify_api_key)):
    """
    Task 6: this used to return {"payment_id": ..., "audit_chain": []}
    unconditionally, for ANY payment_id, valid or not — indistinguishable
    from "this payment genuinely has no audit history."

    STATUS (re-checked in the pre-Phase-8 audit): the audit chain's
    underlying tables — diagnoses, candidate_actions, policy_decisions,
    recoveries, audit_log — are no longer empty. services/pipeline/,
    services/diagnosis_engine/, services/recovery_engine/, and
    workers/execution_worker.py have been writing real rows to all of them
    since Phase 5-7 (proven end-to-end: Phase 7's 10k-payment run, 0
    errors, 966/966 terminal audit_log rows). This endpoint itself is just
    not wired to serve that data yet — the query/response-shape work is
    deferred to Phase 9 (dashboard), not because the data doesn't exist,
    but so the response shape gets designed against Phase 9's actual UI
    needs instead of being guessed at now and redone later. Still 501,
    but the REASON is "not wired," not "nothing to wire to."
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "Audit trail is not implemented yet. The underlying data now "
            "exists (diagnoses, candidate_actions, policy_decisions, "
            "recoveries, and audit_log have been written to since Phase "
            "5-7), but this endpoint is not yet wired to serve it — real "
            "implementation deferred to Phase 9 (dashboard)."
        ),
    )
