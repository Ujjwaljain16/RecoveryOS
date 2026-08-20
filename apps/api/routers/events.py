"""
Event ingestion router — POST /v1/events
Returns 202 immediately; processing happens asynchronously.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from recoveryos.database import get_app_session

router = APIRouter()


class EventPayload(BaseModel):
    payment_id: str
    merchant_id: str
    customer_id: str
    amount_paise: int = Field(gt=0, description="Payment amount in paise (integer, never float)")
    method: str
    bank: str | None = None
    event_type: str
    failure_code: str | None = None


@router.post("", status_code=status.HTTP_202_ACCEPTED, summary="Ingest a payment event")
async def ingest_event(
    payload: EventPayload,
    session: AsyncSession = Depends(get_app_session),
):
    """
    Ingest a payment lifecycle event.
    Returns 202 immediately — risk engine processes asynchronously via Redis stream.
    """
    # Phase 3 implementation: full event processing pipeline.
    # Scaffold: store event and return event_id.
    import uuid
    from recoveryos.models import Event, Payment
    from sqlalchemy import select

    event_id = str(uuid.uuid4())
    # In Phase 3, this will publish to Redis stream for async Risk Engine consumption.
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"event_id": event_id, "status": "accepted"},
    )
