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
    # Compare as bytes: hmac.compare_digest raises TypeError on a str containing
    # non-ASCII characters, which an attacker-controlled header can carry --
    # that would surface as a 500 instead of a clean rejection.
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8", "replace"))


def compute_idempotency_key(raw_body: bytes, event_id: str | None = None) -> str:
    """
    CORRECTED (Domain Audit finding F4, pre-existing false claim): this used
    to say "Razorpay webhook payloads carry no universal unique event-id
    field" and hashed the raw body as the only available dedup key. That
    premise was false -- Razorpay sends a real, unique `X-Razorpay-Event-Id`
    header on every delivery (per Razorpay's own webhook docs), and a
    genuine redelivery of the same event carries the SAME event id, making
    it the correct primary identity: it distinguishes two events whose
    bodies happen to be byte-identical (content-hash cannot), and survives
    Razorpay ever re-serializing a redelivered payload with different byte
    ordering/whitespace for the same logical event (content-hash would
    treat that as two different events).

    `event_id` (the caller-supplied X-Razorpay-Event-Id header value) is
    used verbatim, prefixed to keep the two identity spaces from ever
    colliding, when present. Falls back to the SHA-256 content hash only
    when the header is genuinely absent -- Razorpay's docs don't formally
    guarantee it's sent on every event type/API version, so this must stay
    a real fallback, not dead code.
    """
    if event_id:
        return f"evtid:{event_id}"
    return f"sha256:{hashlib.sha256(raw_body).hexdigest()}"


def _entity(payload: dict, name: str) -> dict:
    """payload["payload"][name]["entity"], or {} if any level is missing or
    not a JSON object. The body is attacker-influenced JSON (post-signature,
    but still untrusted shape); a malformed level must never raise."""
    inner = payload.get("payload")
    if not isinstance(inner, dict):
        return {}
    wrapper = inner.get(name)
    if not isinstance(wrapper, dict):
        return {}
    entity = wrapper.get("entity")
    return entity if isinstance(entity, dict) else {}


def extract_order_id(payload: dict) -> str | None:
    """
    Pulls the Razorpay order id out of whichever entity the event actually
    contains. RazorpayTestAdapter.retry() creates Orders (not raw
    Payments), and stores the resulting order id as recoveries.provider_ref
    — this is the join key reconciliation matches against, so this
    function's whole job is finding that same id inside the webhook body,
    regardless of which entity type carried it.
    """
    order_entity = _entity(payload, "order")
    if isinstance(order_entity.get("id"), str) and order_entity["id"]:
        return order_entity["id"]
    payment_entity = _entity(payload, "payment")
    if isinstance(payment_entity.get("order_id"), str) and payment_entity["order_id"]:
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

    amount = 0
    if outcome == "SUCCESS":
        order_entity = _entity(payload, "order")
        payment_entity = _entity(payload, "payment")
        raw_amount = order_entity.get("amount_paid") or payment_entity.get("amount") or 0
        try:
            amount = int(raw_amount)
        except (TypeError, ValueError):
            amount = 0
        if amount < 0:
            amount = 0
    return outcome, amount
