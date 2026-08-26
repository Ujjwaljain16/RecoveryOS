"""
Razorpay webhook receiver — POST /webhooks/razorpay — Task WEBHOOK1.

Deliberately minimal, per the "don't put AI reasoning inside the webhook
request" principle: receive -> verify -> persist -> deduplicate ->
reconcile (a few indexed lookups, not a decision) -> acknowledge. No
diagnosis, no policy evaluation, no LLM call happens on this path.

Signature verification is over the RAW body bytes, read via request.body()
BEFORE any Pydantic parsing — see integrations/razorpay/webhooks.py's
module docstring for why parse-then-reverify would check the wrong bytes.
Razorpay dashboard docs: test-mode webhook payloads use the identical
structure/signing scheme as live payloads, so this endpoint doesn't need
special-casing for test vs. live traffic.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from integrations.razorpay.webhooks import (
    compute_idempotency_key,
    extract_order_id,
    extract_resolution,
    verify_signature,
)
from recoveryos.config import get_settings
from recoveryos.database import get_app_session
from recoveryos.models import RawWebhookEvent
from services.pipeline.reconciliation import reconcile_pending_recovery

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("", status_code=status.HTTP_200_OK)
async def razorpay_webhook(
    request: Request,
    response: Response,
    x_razorpay_signature: str | None = Header(default=None),
    session: AsyncSession = Depends(get_app_session),
):
    """
    Always returns 200 once the signature check itself has run and the
    event is durably stored (matching Razorpay's own guidance: acknowledge
    quickly, or it retries the same delivery) — EXCEPT an invalid/missing
    signature, which is rejected outright at 401 and never even reaches
    storage. An unrecognized event_type, an order_id RecoveryOS never
    created, or a malformed body are all real, expected, non-error cases
    on a webhook endpoint and get 200 + a stored, clearly-flagged row, not
    a 4xx that would make Razorpay retry forever.
    """
    raw_body = await request.body()
    settings = get_settings()

    if not x_razorpay_signature or not verify_signature(
        raw_body, x_razorpay_signature, settings.razorpay_webhook_secret
    ):
        logger.warning("[RazorpayWebhook] signature verification failed -- rejecting, not storing")
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return {"error": "invalid_signature"}

    idempotency_key = compute_idempotency_key(raw_body)

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None

    event_type = payload.get("event") if isinstance(payload, dict) else None

    existing = await session.execute(
        select(RawWebhookEvent.webhook_event_id).where(
            RawWebhookEvent.idempotency_key == idempotency_key
        )
    )
    if existing.first() is not None:
        logger.info(
            "[RazorpayWebhook] duplicate delivery, idempotency_key=%s -- ack, no-op", idempotency_key
        )
        return {"status": "already_processed"}

    webhook_event_id = str(uuid.uuid4())
    session.add(
        RawWebhookEvent(
            webhook_event_id=webhook_event_id,
            provider="razorpay",
            event_type=event_type,
            raw_payload=payload,
            headers=dict(request.headers),
            signature_verified=True,
            idempotency_key=idempotency_key,
        )
    )
    await session.commit()

    if not isinstance(payload, dict):
        return {"status": "stored", "reconciled": False, "reason": "unparseable_body"}

    order_id = extract_order_id(payload)
    resolution = extract_resolution(event_type, payload) if event_type else None

    if order_id is None or resolution is None:
        return {"status": "stored", "reconciled": False, "reason": "not_a_resolving_event"}

    outcome, recovered_amount_paise = resolution
    matched_recovery_id = await reconcile_pending_recovery(
        session, order_id=order_id, outcome=outcome, recovered_amount_paise=recovered_amount_paise
    )

    await session.execute(
        update(RawWebhookEvent)
        .where(RawWebhookEvent.webhook_event_id == webhook_event_id)
        .values(
            matched_recovery_id=matched_recovery_id,
            reconciliation_note=(
                f"resolved recovery to {outcome}"
                if matched_recovery_id
                else "no PENDING recovery matched this order_id"
            ),
            processed_at=datetime.now(UTC),
        )
    )
    await session.commit()

    return {
        "status": "stored",
        "reconciled": matched_recovery_id is not None,
        "matched_recovery_id": matched_recovery_id,
    }
