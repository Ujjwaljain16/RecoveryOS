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

    501s explicitly instead: the audit chain joins events, diagnoses,
    candidate_actions, policy_decisions, and recoveries — everything past
    `events` is still an empty table (diagnosis_engine/policy_engine/
    recovery_engine don't exist yet), so there is no real audit chain to
    return, honest or otherwise, beyond the raw event log payments.py
    already serves for real.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "Audit trail is not implemented yet. It joins diagnoses, "
            "candidate_actions, policy_decisions, and recoveries — none of "
            "which anything writes to yet. This is an honest 501, not an "
            "empty-but-plausible audit_chain."
        ),
    )
