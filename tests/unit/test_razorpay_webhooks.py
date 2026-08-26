"""
Task WEBHOOK1 -- pure signature-verification and payload-parsing logic
(integrations/razorpay/webhooks.py). No DB, no FastAPI -- these prove the
crypto and extraction logic itself, independent of the endpoint wiring
(tested separately in tests/integration/test_razorpay_webhook_endpoint.py).
"""

from __future__ import annotations

import hashlib
import hmac

from integrations.razorpay.webhooks import (
    compute_idempotency_key,
    extract_order_id,
    extract_resolution,
    verify_signature,
)

SECRET = "test_webhook_secret_do_not_use_in_prod"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_accepts_a_correctly_signed_body():
    body = b'{"event": "payment.failed"}'
    assert verify_signature(body, _sign(body), SECRET) is True


def test_verify_signature_rejects_wrong_signature():
    body = b'{"event": "payment.failed"}'
    assert verify_signature(body, "0" * 64, SECRET) is False


def test_verify_signature_rejects_signature_computed_with_a_different_secret():
    body = b'{"event": "payment.failed"}'
    assert verify_signature(body, _sign(body, secret="wrong-secret"), SECRET) is False


def test_verify_signature_rejects_a_tampered_body():
    """The exact attack this exists to catch: same signature, different
    (tampered) body -- e.g. an attacker changing the amount after a
    legitimate signature was captured."""
    original_body = b'{"event": "payment.captured", "amount": 100}'
    tampered_body = b'{"event": "payment.captured", "amount": 999999}'
    signature = _sign(original_body)
    assert verify_signature(tampered_body, signature, SECRET) is False


def test_verify_signature_fails_closed_when_no_secret_configured():
    """An empty/unconfigured webhook secret must reject everything, never
    accept-by-default -- the fail-closed contract this whole endpoint
    depends on."""
    body = b'{"event": "payment.failed"}'
    assert verify_signature(body, _sign(body, secret=""), "") is False


def test_compute_idempotency_key_is_deterministic():
    body = b'{"event": "payment.failed", "x": 1}'
    assert compute_idempotency_key(body) == compute_idempotency_key(body)


def test_compute_idempotency_key_differs_for_different_bodies():
    key_a = compute_idempotency_key(b'{"event": "payment.failed"}')
    key_b = compute_idempotency_key(b'{"event": "payment.captured"}')
    assert key_a != key_b


def test_extract_order_id_from_order_entity():
    payload = {"payload": {"order": {"entity": {"id": "order_ABC123"}}}}
    assert extract_order_id(payload) == "order_ABC123"


def test_extract_order_id_from_payment_entity_order_id_field():
    payload = {"payload": {"payment": {"entity": {"order_id": "order_XYZ789"}}}}
    assert extract_order_id(payload) == "order_XYZ789"


def test_extract_order_id_prefers_order_entity_when_both_present():
    payload = {
        "payload": {
            "order": {"entity": {"id": "order_FROM_ORDER"}},
            "payment": {"entity": {"order_id": "order_FROM_PAYMENT"}},
        }
    }
    assert extract_order_id(payload) == "order_FROM_ORDER"


def test_extract_order_id_returns_none_when_absent():
    assert extract_order_id({"payload": {}}) is None
    assert extract_order_id({}) is None


def test_extract_resolution_payment_captured():
    payload = {"payload": {"payment": {"entity": {"amount": 150000}}}}
    assert extract_resolution("payment.captured", payload) == ("SUCCESS", 150000)


def test_extract_resolution_order_paid_prefers_amount_paid():
    payload = {"payload": {"order": {"entity": {"amount_paid": 200000, "amount": 200000}}}}
    assert extract_resolution("order.paid", payload) == ("SUCCESS", 200000)


def test_extract_resolution_payment_failed_has_zero_amount():
    payload = {"payload": {"payment": {"entity": {"amount": 150000}}}}
    assert extract_resolution("payment.failed", payload) == ("FAILED", 0)


def test_extract_resolution_none_for_unrecognized_event():
    payload = {"payload": {"subscription": {"entity": {"id": "sub_1"}}}}
    assert extract_resolution("subscription.activated", payload) is None
