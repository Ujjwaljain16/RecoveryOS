"""
Customer opt-out — the one mutation both a real "stop contacting me" action
(apps/api/routers/customers.py) and the synthetic dataset's own opt-out
generation (simulator/run.py's customer-persistence step) apply, so
OptOutRule (services/policy_engine/rules.py) gets exercised by real traffic
and by the 10k-payment eval dataset through the SAME code path (gaps.md
sec:A.1) rather than two independently-maintained ones that could silently
drift apart -- the failure mode this file exists to close is "the live
endpoint sets one thing, the simulator sets something structurally
different, and nobody notices until a demo."

Deliberately I/O-free: takes an already-fetched/constructed `Customer` ORM
object and mutates it in place, returning the AuditLog row to persist (or
None if there's nothing new to write). The caller owns the session --
fetching, adding, and committing look different for the async FastAPI route
vs. the sync simulator persistence loop, but the actual opt-out logic
(idempotency check, timestamp, audit summary) must be identical either way.
"""

from __future__ import annotations

from datetime import datetime

from recoveryos.models import AuditLog, Customer

VALID_CHANNELS = ("sms", "email", "support_call")


def apply_customer_opt_out(
    customer: Customer,
    *,
    now: datetime,
    reason: str | None = None,
    channel: str | None = None,
) -> AuditLog | None:
    """
    Idempotent: if `customer` is already opted out, this is a no-op --
    `customer.opted_out_at` is left untouched (never overwritten with a
    later timestamp) and None is returned (no new audit row). Otherwise
    sets `customer.opted_out_at = now` and returns an AuditLog row ready to
    be added to the session -- payment_id is deliberately None (an opt-out
    is a customer-level action, not tied to any specific payment; see
    migration 0023's own docstring for why audit_log gained a customer_id
    column rather than trying to force this through events, whose
    payment_id is NOT NULL).
    """
    if customer.opted_out_at is not None:
        return None

    customer.opted_out_at = now
    channel_desc = channel or "unspecified channel"
    summary = f"Customer opted out of recovery contact via {channel_desc}"
    if reason:
        summary += f" — {reason}"
    return AuditLog(customer_id=customer.customer_id, payment_id=None, summary=summary)
