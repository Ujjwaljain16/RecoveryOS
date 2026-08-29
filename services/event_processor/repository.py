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

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from recoveryos.models import Event, EventPublication, Payment

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


# Live E2E smoke test finding (2026-08-29): a REAL payment ingested through
# /v1/events -> event_processor never got a failure_class at all -- this
# upsert only ever wrote failure_code, and EventPayload has no failure_class
# field to send one through (merchants report a failure CODE, not our
# internal TEMPORARY/PERMANENT/CUSTOMER_SPECIFIC taxonomy). Every real,
# non-simulator-seeded payment then hit
# services.recovery_engine.propensity.build_propensity_context's hard
# ValueError ("required feature missing: initial_failure_class") the moment
# it reached decisioning -- the demo's actual live-payment path was broken
# for anything other than internally-seeded synthetic data.
#
# Keyword classification against the SAME three classes
# services/pipeline/baseline.py's BASELINE_UNRETRYABLE_FAILURE_CLASSES and
# simulator/failures/codes.py's ObservedFailureClass already use -- not a
# new taxonomy. failure_code is open-ended free text at the API boundary
# (EventPayload's own `pattern` only bounds its shape, not its vocabulary),
# so this can never be a closed lookup table; TEMPORARY is the deliberate
# default for anything unrecognized (same "assume retryable unless proven
# otherwise" stance _would_baseline_retry already takes with its
# PERMANENT-only blocklist), not a guess dressed up as certainty.
_PERMANENT_KEYWORDS = ("PERMANENT", "INVALID", "EXPIRED", "CLOSED", "BLOCKED", "FRAUD")
_CUSTOMER_SPECIFIC_KEYWORDS = ("CUSTOMER", "INSUFFICIENT_FUNDS", "AUTH_EXHAUSTED", "LIMIT_EXCEEDED")


def classify_failure(failure_code: str | None) -> str:
    """Best-effort failure_class from a real merchant's failure_code --
    see the module comment above for why this exists and why TEMPORARY is
    the safe default rather than leaving the field NULL."""
    if not failure_code:
        return "TEMPORARY"
    code = failure_code.upper()
    if any(kw in code for kw in _PERMANENT_KEYWORDS):
        return "PERMANENT"
    if any(kw in code for kw in _CUSTOMER_SPECIFIC_KEYWORDS):
        return "CUSTOMER_SPECIFIC"
    return "TEMPORARY"


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
            failure_class=classify_failure(msg.get("failure_code")),
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
) -> tuple[str, bool]:
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

    Returns (resolved_event_id, is_new). is_new is True if this exact
    (payment_id, idempotency_key) hadn't been seen before — informational
    only now (Task S4, pre-Phase-8 audit): the CALLER's publish decision
    must be based on event_publications, not on is_new, since a publish can
    legitimately still be pending for an event that was already inserted on
    a prior, failed attempt. resolved_event_id is the WINNING row's real
    event_id — msg["event_id"] itself is only correct when is_new is True;
    on a duplicate, the row that actually exists may have a different
    event_id from an earlier POST of the same idempotency_key.
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
    row = result.first()

    if row is not None:
        return row[0], True

    logger.info(
        "[Repo] Duplicate event skipped: idempotency_key=%s (event_id=%s)",
        idempotency_key,
        event_id,
    )
    existing = await session.execute(
        select(Event.event_id).where(
            Event.payment_id == msg["payment_id"], Event.idempotency_key == idempotency_key
        )
    )
    return existing.scalar_one(), False


async def is_event_published(session: AsyncSession, event_id: str) -> bool:
    """Has this event's downstream publish (stream:risk_engine) actually
    succeeded before? See EventPublication's docstring (recoveryos/models.py)
    for why this is a separate INSERT-only table, not a column on events."""
    result = await session.execute(
        select(EventPublication.event_id).where(EventPublication.event_id == event_id)
    )
    return result.first() is not None


async def mark_event_published(session: AsyncSession, event_id: str) -> None:
    """Record that event_id's downstream publish just succeeded. INSERT
    only — event_publications is never updated, mirroring events' own
    append-only discipline."""
    stmt = (
        pg_insert(EventPublication)
        .values(event_id=event_id)
        .on_conflict_do_nothing(index_elements=["event_id"])
    )
    await session.execute(stmt)
