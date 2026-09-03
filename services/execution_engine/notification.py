"""
Notification abstraction — Domain Audit finding #3.

Before this: workers/execution_worker.py called the exact same
PaymentProvider.retry() (a real charge/order-creation call) for EVERY
action_type, including REMINDER -- even though services/policy_engine/
rules.py's own QuietHoursComplianceRule docstring calls REMINDER "the only
customer-contact action in this system" and gates it on quiet hours, not
on the payment provider's execution window. A decision to merely notify
the customer was silently executing a real attempted charge instead.

This module is the real boundary: REMINDER routes here, never to
PaymentProvider.retry(). Returns the same ProviderResult shape
integrations/razorpay/adapter.py's PaymentProvider does, so
workers/execution_worker.py's existing _upsert_recovery/_write_ledger_and_
audit machinery works unchanged -- recovered_amount_paise is ALWAYS 0 here
(a notification never itself recovers money; see services/pipeline/
ledger.py's _should_correct_ledger invariant, which correctly treats a
0-recovery outcome as carrying no new revenue information regardless of
this action's own SUCCESS/FAILED label).

Demo scope, stated honestly per the audit's own instruction: a safe,
observable STUB, not a half-built SMS/email integration. Records a real
`events` row (event_type='NOTIFICATION_SENT') -- the exact same append-
only event-log pattern execution_worker.py already uses for
RECOVERY_SCHEDULED/EXECUTING/etc -- with status='simulated', never 'sent'.
Nothing here claims a real message left this system.

amount_paise is accepted but unused in send_reminder() -- kept only so
this Protocol's call shape matches PaymentProvider.retry()'s
(payment_id, amount_paise, attempt_number), letting workers/
execution_worker.py dispatch to either without special-casing REMINDER.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection

Outcome = str  # "SUCCESS" | "FAILED" -- matches integrations/razorpay/adapter.py's Outcome


@dataclass(frozen=True)
class NotificationResult:
    outcome: Outcome
    provider_ref: str | None
    recovered_amount_paise: int  # ALWAYS 0 -- a notification never itself recovers money


class NotificationService(Protocol):
    def send_reminder(
        self, conn: Connection, payment_id: str, amount_paise: int, attempt_number: int
    ) -> NotificationResult: ...


def _now() -> datetime:
    return datetime.now(UTC)


class DemoNotificationAdapter:
    """
    The one real implementation today. Records a `PAYMENT_RETRY_REMINDER`
    notification as a real `events` row with status='simulated' -- never
    calls any real SMS/email/push provider. `channel` is fixed to 'email'
    (the one channel this demo models); a real implementation would derive
    it from customer preference.
    """

    CHANNEL = "email"

    def send_reminder(
        self, conn: Connection, payment_id: str, amount_paise: int, attempt_number: int
    ) -> NotificationResult:
        event_id = str(uuid.uuid4())
        idempotency_key = f"notification:{payment_id}:{attempt_number}"
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
                "event_type": "NOTIFICATION_SENT",
                "payload": json.dumps(
                    {
                        "notification_type": "PAYMENT_RETRY_REMINDER",
                        "channel": self.CHANNEL,
                        "status": "simulated",
                        "attempt_number": attempt_number,
                    }
                ),
                "occurred_at": _now(),
            },
        )
        return NotificationResult(
            outcome="SUCCESS", provider_ref=event_id, recovered_amount_paise=0
        )


def get_notification_service() -> NotificationService:
    return DemoNotificationAdapter()
