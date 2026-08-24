"""
Event Processor Service — Postgres Repository
=============================================
Responsible for all DB writes from the event processor.

Design contracts:
  - Payment dedup: ON CONFLICT DO NOTHING on payments.payment_id.
  - Event dedup: ON CONFLICT DO NOTHING on (events.payment_id, events.idempotency_key)
    — the client-supplied key (or the event_id fallback if the client sent
    none), scoped per-payment, NOT the server-minted event_id (fresh on every
    POST, so it can't itself detect a client-side retry) and NOT a
    globally-unique key (a key reused across unrelated payments must not
    silently drop a legitimate event on the other payment). See
    insert_event_idempotent().
  - All writes are within a single transaction per event.
  - Payment upsert: creates the payment record if not yet in DB (API-first ingest path
    means events can arrive before the payment is explicitly created).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from recoveryos.models import Event, Payment

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


async def upsert_payment(session: AsyncSession, msg: dict[str, Any]) -> None:
    """
    Upsert a Payment row from an inbound event.

    Uses INSERT ... ON CONFLICT DO NOTHING so that:
    - If the payment already exists (e.g. created by the simulator or a prior event),
      this is a no-op.
    - If the payment doesn't exist (API-first path), we create a minimal record.

    The UNIQUE constraint on payment_id is the physical idempotency backstop.
    """
    stmt = (
        pg_insert(Payment)
        .values(
            payment_id=msg["payment_id"],
            merchant_id=msg["merchant_id"],
            customer_id=msg["customer_id"],
            amount_paise=int(msg["amount_paise"]),
            method=msg["method"],
            bank=msg.get("bank") or None,
            status="failed" if msg.get("event_type") == "PAYMENT_FAILED" else "created",
            failure_code=msg.get("failure_code") or None,
            is_synthetic=False,  # HTTP path = live/real events
            created_at=_now(),
            failed_at=_now() if msg.get("event_type") == "PAYMENT_FAILED" else None,
        )
        .on_conflict_do_nothing(index_elements=["payment_id"])
    )
    await session.execute(stmt)


async def insert_event_idempotent(
    session: AsyncSession,
    msg: dict[str, Any],
) -> bool:
    """
    Insert an Event row, idempotent on the CLIENT-supplied idempotency_key
    (falling back to the server-minted event_id when the client sent none —
    see EventPayload.idempotency_key in apps/api/routers/events.py).

    event_id is minted fresh by the API on every single POST (events.py:138),
    so it can never be used as the dedup key for a client-side retry with the
    same idempotency_key: two POSTs with an identical idempotency_key arrive
    here as two distinct event_ids, and only idempotency_key ties them
    together. Dedup is therefore keyed on (payment_id, idempotency_key),
    backed by the UNIQUE constraint on events(payment_id, idempotency_key)
    (migration 0005) via INSERT ... ON CONFLICT DO NOTHING — atomic, not
    check-then-insert, so it is race-safe against a concurrent redelivery of
    the same message. Scoped per-payment rather than globally so a key
    accidentally reused across two different payments doesn't cause the
    second payment's real event to be silently dropped.

    Returns True if a new row was inserted, False if this is a duplicate.
    The caller uses this to decide whether to publish downstream.
    """
    event_id = msg["event_id"]
    idempotency_key = msg.get("idempotency_key") or event_id

    payload = {
        "event_id": event_id,
        "idempotency_key": idempotency_key,
        "payment_id": msg["payment_id"],
        "merchant_id": msg["merchant_id"],
        "method": msg["method"],
        "bank": msg.get("bank") or None,
        "amount_paise": int(msg["amount_paise"]),
        "failure_code": msg.get("failure_code") or None,
    }

    stmt = (
        pg_insert(Event)
        .values(
            event_id=event_id,
            payment_id=msg["payment_id"],
            idempotency_key=idempotency_key,
            event_type=msg["event_type"],
            payload=payload,
            occurred_at=_now(),
        )
        .on_conflict_do_nothing(index_elements=["payment_id", "idempotency_key"])
        .returning(Event.event_id)
    )
    result = await session.execute(stmt)
    inserted = result.first() is not None

    if not inserted:
        logger.info(
            "[Repo] Duplicate event skipped: idempotency_key=%s (event_id=%s)",
            idempotency_key,
            event_id,
        )

    return inserted
