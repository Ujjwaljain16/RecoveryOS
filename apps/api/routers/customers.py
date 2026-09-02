"""Customer router — POST /v1/customers/{customer_id}/opt-out (gaps.md sec:A.1).

Dual-path by design (gaps.md sec:A.1): this is the ONE real endpoint both a
genuine "stop contacting me" webhook/UI action AND simulator/customers/
generator.py's own opt-out generation are meant to go through, so
OptOutRule (services/policy_engine/rules.py, already reads
customer.opted_out_at IS NULL, unchanged by this file) is exercised by real
traffic and by the synthetic dataset through the SAME code path, not two
independently-maintained ones that could silently drift apart.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies.auth import verify_api_key
from recoveryos import clock
from recoveryos.database import get_app_session
from recoveryos.models import Customer, Merchant
from services.customer_engine.opt_out import apply_customer_opt_out

router = APIRouter()


class OptOutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None
    channel: Literal["sms", "email", "support_call"] | None = None


@router.post("/{customer_id}/opt-out", summary="Customer opts out of further recovery contact")
async def opt_out_customer(
    customer_id: str,
    payload: OptOutRequest,
    merchant: Merchant = Depends(verify_api_key),
    session: AsyncSession = Depends(get_app_session),
):
    """
    Idempotent: re-calling on an already-opted-out customer returns 200 with
    the ORIGINAL opted_out_at, unchanged — never overwrites the timestamp,
    never errors. The next policy evaluation for any of this customer's
    payments blocks via OptOutRule immediately; no separate wiring needed
    there, it already reads this exact column.

    Scoped to the authenticated merchant — a customer_id belonging to a
    DIFFERENT merchant (or not existing at all) both 404 identically,
    matching payments.py's own established pattern, so a caller can't
    enumerate other merchants' customer_ids by probing.
    """
    customer = await session.get(Customer, customer_id)
    if customer is None or customer.merchant_id != merchant.merchant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")

    audit_row = apply_customer_opt_out(
        customer, now=clock.utcnow(), reason=payload.reason, channel=payload.channel
    )
    if audit_row is not None:
        session.add(audit_row)
        await session.commit()

    return {"customer_id": customer.customer_id, "opted_out_at": customer.opted_out_at.isoformat()}
