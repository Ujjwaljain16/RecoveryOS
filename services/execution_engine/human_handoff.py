"""
Human-handoff abstraction — Domain Audit finding #3. Same reasoning as
services/execution_engine/notification.py: ESCALATE means "route to a
human/compliance review" (see services/policy_engine/rules.py's
RetryLimitRule docstring), not "attempt a real charge." Routes here,
never to PaymentProvider.retry().

recovered_amount_paise is ALWAYS 0 -- creating an escalation record never
itself recovers money.

amount_paise is accepted but unused in create_escalation() -- kept only so
this Protocol's call shape matches PaymentProvider.retry()'s
(payment_id, amount_paise, attempt_number), letting workers/
execution_worker.py dispatch to either without special-casing ESCALATE.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection

Outcome = str


@dataclass(frozen=True)
class HandoffResult:
    outcome: Outcome
    provider_ref: str | None
    recovered_amount_paise: int  # ALWAYS 0


class HumanHandoffService(Protocol):
    def create_escalation(
        self, conn: Connection, payment_id: str, amount_paise: int, attempt_number: int
    ) -> HandoffResult: ...


def _now() -> datetime:
    return datetime.now(UTC)


class DemoHumanHandoffAdapter:
    """
    Records a real `events` row (event_type='ESCALATION_CREATED',
    status='recorded') -- the queue/inbox a human reviewer would actually
    work from is out of this demo's scope, but the record itself is real,
    queryable, and never a disguised payment-provider call.
    """

    def create_escalation(
        self, conn: Connection, payment_id: str, amount_paise: int, attempt_number: int
    ) -> HandoffResult:
        event_id = str(uuid.uuid4())
        idempotency_key = f"escalation:{payment_id}:{attempt_number}"
        conn.execute(
            text(
                "INSERT INTO events (event_id, payment_id, idempotency_key, event_type, payload, occurred_at) "
                "VALUES (:event_id, :payment_id, :idempotency_key, :event_type, :payload, :occurred_at) "
                "ON CONFLICT (payment_id, idempotency_key) DO NOTHING"
            ),
            {
                "event_id": event_id,
                "payment_id": payment_id,
                "idempotency_key": idempotency_key,
                "event_type": "ESCALATION_CREATED",
                "payload": json.dumps(
                    {
                        "reason": "policy decision ESCALATE",
                        "status": "recorded",
                        "attempt_number": attempt_number,
                    }
                ),
                "occurred_at": _now(),
            },
        )
        return HandoffResult(outcome="SUCCESS", provider_ref=event_id, recovered_amount_paise=0)


def get_human_handoff_service() -> HumanHandoffService:
    return DemoHumanHandoffAdapter()
