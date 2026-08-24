"""
Event ingestion router — POST /v1/events
TRD §5 contract: exact request/response shape.

Flow:
  1. Pydantic validates request body (strict mode).
  2. RateLimiter dependency checks merchant token bucket (Redis).
  3. event_id generated (UUID4).
  4. Payload published to Redis stream:payment_failed via XADD.
  5. 202 Accepted returned immediately.

Async semantics:
  - This endpoint is FAST (Redis XADD only, no DB write).
  - DB write happens in the consumer (services/event_processor/consumer.py).
  - Idempotency is enforced at the consumer/repository layer via a UNIQUE
    constraint on events(payment_id, idempotency_key) (migration 0005),
    scoped per-payment rather than globally so a key accidentally reused
    across two different payments can't cause the second payment's real
    event to be silently dropped. Keyed on the client-supplied
    idempotency_key from the request BODY (see EventPayload.idempotency_key
    below — a body field, not a header, because it names the specific
    logical event being retried, not something that applies to the whole
    request/connection), falling back to the event_id if omitted — see
    services/event_processor/repository.py:insert_event_idempotent.
    Delivery itself is at-least-once (Redis Streams); the Postgres write is
    the idempotent sink, not the transport.

Auth (Task 4):
  - Every request must present a valid X-API-Key (apps/api/dependencies/auth.py).
    Missing/invalid key → 401, before the request body is even looked at.
  - The merchant identity used for rate limiting AND as the canonical
    merchant_id written downstream is ALWAYS the verified `merchant` from
    verify_api_key — never payload.merchant_id. payload.merchant_id is still
    part of the request contract (a client states which merchant it believes
    it's acting as), but it's now checked AGAINST the verified identity, not
    trusted as the identity itself: a mismatch means either a client bug or
    an attempt to submit events under a different merchant's name using
    merchant A's own key, and gets a 403 either way.
"""

from __future__ import annotations

import uuid

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from apps.api.dependencies.auth import verify_api_key
from apps.api.dependencies.rate_limit import RateLimiter
from recoveryos.models import Merchant
from recoveryos.redis import get_redis

router = APIRouter()

# ─── Rate limiter instance (shared across requests) ────────────────────────────
_rate_limiter = RateLimiter(capacity=1000, refill_rate=500)

# ─── Redis stream constants ────────────────────────────────────────────────────
STREAM_PAYMENT_FAILED = "stream:payment_failed"
STREAM_MAXLEN = 100_000  # ~trim to last 100k messages; see notes below
# NOTE on MAXLEN + durability:
#   Redis Streams with MAXLEN trim only affect messages that have ALREADY been
#   XACK'd by all consumer groups. Unacknowledged (pending) messages are NOT
#   evicted regardless of MAXLEN. This means:
#   - If the consumer is down and messages pile up past MAXLEN, older ACK'd
#     messages are trimmed but unprocessed pending messages remain.
#   - Postgres events ledger is the durable source of record; Redis is transport.
#   - After the consumer processes + ACKs a message, it's safe to trim.


# ─── Request/Response Schemas ─────────────────────────────────────────────────
class EventPayload(BaseModel):
    """
    TRD §5: POST /v1/events request body — strict contract.
    All fields are required unless explicitly marked Optional.
    """

    model_config = {"extra": "forbid"}  # Reject unexpected fields → 422

    payment_id: str = Field(
        description="Payment identifier. Must be a valid UUID from the payments table.",
        examples=["a1b2c3d4-e5f6-7890-abcd-ef1234567890"],
    )
    merchant_id: str = Field(
        description=(
            "Merchant identifier. Must match the merchant resolved from the "
            "X-API-Key header — a mismatch is rejected with 403, regardless "
            "of whether payment_id/customer_id would otherwise be valid."
        ),
    )
    customer_id: str = Field(description="Customer identifier.")
    amount_paise: int = Field(
        gt=0,
        description="Payment amount in paise (integer only, never float). ₹100 = 10000.",
    )
    method: str = Field(
        description="Payment method: upi | card | netbanking | wallet",
        pattern=r"^(upi|card|netbanking|wallet)$",
    )
    bank: str | None = Field(
        default=None,
        description="Issuing bank identifier. Optional for wallet payments.",
    )
    event_type: str = Field(
        description="Event type: PAYMENT_FAILED | PAYMENT_CREATED | RETRY_EXECUTED | ...",
    )
    failure_code: str | None = Field(
        default=None,
        description="Failure code if event_type is PAYMENT_FAILED.",
    )
    idempotency_key: str | None = Field(
        default=None,
        description=(
            "Client-supplied idempotency key. If provided, duplicate events with the "
            "same key are silently accepted (202) but not double-processed. "
            "If omitted, the server-generated event_id serves as the key."
        ),
    )


class EventAccepted(BaseModel):
    event_id: str
    stream_id: str
    status: str = "accepted"


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=EventAccepted,
    summary="Ingest a payment event (TRD §5)",
    description=(
        "Ingest a payment lifecycle event. Returns 202 immediately; "
        "processing (DB write, downstream publish) is asynchronous via Redis stream."
    ),
)
async def ingest_event(
    payload: EventPayload,
    # verify_api_key is depended on TWICE in this chain (directly here, and
    # again inside RateLimiter.__call__) — FastAPI caches dependency results
    # per request, so the DB lookup happens once, and both call sites always
    # see the same verified Merchant, never two independently-resolved ones.
    merchant: Merchant = Depends(verify_api_key),
    _rate_limit: None = Depends(_rate_limiter),
    redis: aioredis.Redis = Depends(get_redis),
) -> JSONResponse:
    """
    SECURITY: the caller's identity is `merchant`, resolved by verify_api_key
    from a real X-API-Key lookup — never trusted from anything the client
    merely states. payload.merchant_id is checked AGAINST that verified
    identity (anti-spoofing with something real behind it now, not two
    unverified client-supplied values compared to each other).
    """
    if payload.merchant_id != merchant.merchant_id:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "error": "merchant_identity_mismatch",
                "detail": (
                    "Body merchant_id does not match the merchant resolved from "
                    "your API key. You cannot submit events on behalf of "
                    "another merchant."
                ),
            },
        )

    event_id = str(uuid.uuid4())
    idempotency_key = payload.idempotency_key or event_id

    # Publish to Redis stream — this is the ONLY write in the hot path.
    # merchant_id comes from the VERIFIED identity, not payload.merchant_id —
    # they're equal at this point (checked above), but downstream code should
    # never have to re-derive that trust boundary from a request body field.
    message = {
        "event_id": event_id,
        "idempotency_key": idempotency_key,
        "payment_id": payload.payment_id,
        "merchant_id": merchant.merchant_id,
        "customer_id": payload.customer_id,
        "amount_paise": str(payload.amount_paise),
        "method": payload.method,
        "bank": payload.bank or "",
        "event_type": payload.event_type,
        "failure_code": payload.failure_code or "",
    }

    stream_id: str = await redis.xadd(
        STREAM_PAYMENT_FAILED,
        message,
        maxlen=STREAM_MAXLEN,
        approximate=True,  # ~ trim — faster, keeps ordering guarantees
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=EventAccepted(
            event_id=event_id,
            stream_id=stream_id,
        ).model_dump(),
    )
