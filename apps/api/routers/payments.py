"""Payment detail router — GET /v1/payments/{payment_id}/detail"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies.auth import verify_api_key
from recoveryos.database import get_app_session
from recoveryos.models import Event, Merchant, Payment

router = APIRouter()


@router.get("/{payment_id}/detail", summary="Full decision chain for a payment")
async def payment_detail(
    payment_id: str,
    merchant: Merchant = Depends(verify_api_key),
    session: AsyncSession = Depends(get_app_session),
):
    """
    Task 6: real, minimal implementation — this used to be a pure stub that
    took a live DB session as a dependency and never touched it, always
    returning {"payment": None, "diagnosis": None, ...} regardless of
    whether the payment existed. Indistinguishable, to a caller, from a
    genuinely-empty result.

    Now backed by the actual `payments` and `events` tables, which the real
    ingest path (services/event_processor) has been writing to since Task 2
    — so there's real data to serve. `diagnosis`, `candidate_actions`,
    `policy_decision`, and `recovery_history` are still reported as
    empty/null, but HONESTLY: nothing writes to those tables yet
    (diagnosis_engine, policy_engine, recovery_engine are all still empty
    packages), so returning empty is the truth, not a placeholder standing
    in for a real query nobody wrote.

    Scoped to the authenticated merchant — a payment_id belonging to a
    DIFFERENT merchant (or not existing at all) both 404 identically. Never
    distinguish "doesn't exist" from "exists but isn't yours": doing so
    would let a caller enumerate other merchants' payment_ids by probing
    IDs and reading the status code.
    """
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.merchant_id != merchant.merchant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found.")

    event_rows = (
        (
            await session.execute(
                select(Event).where(Event.payment_id == payment_id).order_by(Event.occurred_at)
            )
        )
        .scalars()
        .all()
    )

    return {
        "payment_id": payment.payment_id,
        "payment": {
            "customer_id": payment.customer_id,
            "amount_paise": payment.amount_paise,
            "method": payment.method,
            "bank": payment.bank,
            "status": payment.status,
            "failure_code": payment.failure_code,
            "failure_class": payment.failure_class,
            "created_at": payment.created_at.isoformat(),
            "failed_at": payment.failed_at.isoformat() if payment.failed_at else None,
        },
        "events": [
            {
                "event_id": e.event_id,
                "event_type": e.event_type,
                "occurred_at": e.occurred_at.isoformat(),
            }
            for e in event_rows
        ],
        # Honestly empty — no diagnosis/policy/recovery engine writes to
        # these tables yet. Not a hardcoded stand-in for a query that
        # doesn't exist.
        "diagnosis": None,
        "candidate_actions": [],
        "policy_decision": None,
        "recovery_history": [],
    }
