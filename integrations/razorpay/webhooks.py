"""
Razorpay webhook verification and parsing — Task WEBHOOK1.

Pure functions, zero I/O, zero DB — the same purity discipline as
services/policy_engine/rules.py, for the same reason: every one of these is
independently unit-testable with hand-built bytes/dicts, no container needed
to prove the crypto and parsing logic itself is correct.

Signature verification is the ONE non-negotiable rule per Razorpay's own
docs: HMAC-SHA256 over the RAW request body bytes, computed BEFORE any JSON
parsing. Parsing first and re-serializing to verify against would check a
different byte sequence than what Razorpay actually signed (whitespace,
key ordering, unicode escaping can all differ) — apps/api/routers/
razorpay_webhooks.py reads request.body() directly for exactly this reason,
never a parsed-then-reconstructed model.
"""

from __future__ import annotations

import hashlib
import hmac


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """
    True iff `signature` (the X-Razorpay-Signature header value) matches
    HMAC-SHA256(secret, raw_body). Uses hmac.compare_digest — a naive `==`
    on the hex digests would leak timing information about how many
    leading characters matched, letting an attacker forge a valid
    signature one byte at a time.
    """
    if not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def compute_idempotency_key(raw_body: bytes) -> str:
    """
    SHA-256 of the raw body bytes. Razorpay's webhook payloads carry no
    universal unique event-id field (unlike Stripe's `id: evt_xxx`), so a
    content hash is the real, always-available dedup key: Razorpay retries
    a webhook delivery verbatim on any non-2xx response, so a genuine
    redelivery hashes identically; a different event (even the same
    event_type moments later) never collides.
    """
    return hashlib.sha256(raw_body).hexdigest()


def extract_order_id(payload: dict) -> str | None:
    """
    Pulls the Razorpay order id out of whichever entity the event actually
    contains. RazorpayTestAdapter.retry() creates Orders (not raw
    Payments), and stores the resulting order id as recoveries.provider_ref
    — this is the join key reconciliation matches against, so this
    function's whole job is finding that same id inside the webhook body,
    regardless of which entity type carried it.
    """
    inner = payload.get("payload", {})
    order_entity = inner.get("order", {}).get("entity", {})
    if order_entity.get("id"):
        return order_entity["id"]
    payment_entity = inner.get("payment", {}).get("entity", {})
    if payment_entity.get("order_id"):
        return payment_entity["order_id"]
    return None


# Razorpay events that resolve a PENDING recovery to a real terminal
# outcome. Anything else (subscription lifecycle, refunds, etc.) is stored
# for audit but not reconciled against recoveries -- out of this task's
# scope (see migration 0016's docstring).
_RESOLVING_EVENTS = {
    "payment.captured": "SUCCESS",
    "order.paid": "SUCCESS",
    "payment.failed": "FAILED",
}


def extract_resolution(event_type: str, payload: dict) -> tuple[str, int] | None:
    """
    (outcome, recovered_amount_paise) if this event_type resolves a
    PENDING recovery to a terminal state, else None. amount is read from
    whichever entity is present (order or payment) -- Razorpay reports
    amount in the same paise-equivalent integer unit RecoveryOS already
    uses (INR's smallest unit), so no conversion is needed here beyond
    trusting Razorpay's own integer.
    """
    outcome = _RESOLVING_EVENTS.get(event_type)
    if outcome is None:
        return None

    inner = payload.get("payload", {})
    amount = 0
    if outcome == "SUCCESS":
        order_entity = inner.get("order", {}).get("entity", {})
        payment_entity = inner.get("payment", {}).get("entity", {})
        amount = order_entity.get("amount_paid") or payment_entity.get("amount") or 0
    return outcome, int(amount)
