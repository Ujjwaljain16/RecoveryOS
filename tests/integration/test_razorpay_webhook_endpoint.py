"""
Task WEBHOOK1 -- the full POST /webhooks/razorpay path, real FastAPI app
(ASGITransport, no live server), real Postgres. Proves: real signature
verification rejects/accepts correctly, the raw event is durably stored
regardless of whether it resolves anything, idempotency holds across a
redelivered identical body, and a genuinely PENDING recovery (created the
same way RazorpayTestAdapter.retry() creates one) gets reconciled to a
real terminal recovery_ledger row.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import seed_merchant_and_customer, to_async_url

WEBHOOK_SECRET = "test-razorpay-webhook-secret"


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _seed_pending_recovery(migrated_db: str, *, order_id: str, amount_paise: int) -> str:
    """Mirrors exactly what RazorpayTestAdapter.retry() + execution_worker
    leave behind for a real order: a payment, a real diagnosis/candidate/
    policy_decision chain, and a recoveries row with outcome=PENDING and
    provider_ref=the real order id."""
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    engine = create_async_engine(to_async_url(migrated_db))
    payment_id = str(uuid.uuid4())
    candidate_id = str(uuid.uuid4())
    decision_id = str(uuid.uuid4())
    policy_config_id = str(uuid.uuid4())
    recovery_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, :amount, 'upi', 'HDFC', 'failed', 'TIMEOUT', 'TEMPORARY', "
                "true, :ts, :ts)"
            ),
            {"pid": payment_id, "mid": merchant_id, "cid": customer_id, "amount": amount_paise,
             "ts": datetime.now(UTC) - timedelta(hours=1)},
        )
        await conn.execute(
            text("INSERT INTO policy_configs (policy_config_id) VALUES (:pcid)"),
            {"pcid": policy_config_id},
        )
        await conn.execute(
            text(
                "INSERT INTO candidate_actions (candidate_id, payment_id, action_type, "
                "recovery_prob_bps, expected_value_paise, cost_paise, friction_penalty_paise, "
                "risk_penalty_paise, model_version, created_at) "
                "VALUES (:cid, :pid, 'RETRY_NOW', 8000, 80000, 0, 0, 0, 'test-v1', now())"
            ),
            {"cid": candidate_id, "pid": payment_id},
        )
        await conn.execute(
            text(
                "INSERT INTO policy_decisions (decision_id, payment_id, candidate_id, "
                "policy_config_id, verdict, rule_trace, created_at) "
                "VALUES (:did, :pid, :cid, :pcid, 'ALLOW', '[]'::jsonb, now())"
            ),
            {"did": decision_id, "pid": payment_id, "cid": candidate_id, "pcid": policy_config_id},
        )
        await conn.execute(
            text(
                "INSERT INTO recoveries (recovery_id, payment_id, decision_id, idempotency_key, "
                "attempt_number, action_type, scheduled_for, executed_at, outcome, "
                "recovered_amount_paise, provider_ref, created_at) "
                "VALUES (:rid, :pid, :did, :ik, 1, 'RETRY_NOW', now(), NULL, 'PENDING', 0, :oid, now())"
            ),
            {"rid": recovery_id, "pid": payment_id, "did": decision_id,
             "ik": f"recovery:{payment_id}:RETRY_NOW:1", "oid": order_id},
        )
    await engine.dispose()
    return payment_id


@pytest.mark.asyncio
async def test_invalid_signature_rejected_and_not_stored(async_client, migrated_db, monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    body = json.dumps({"event": "payment.failed", "payload": {}}).encode()
    resp = await async_client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"X-Razorpay-Signature": "0" * 64, "Content-Type": "application/json"},
    )
    get_settings.cache_clear()

    assert resp.status_code == 401

    from sqlalchemy import create_engine

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM raw_webhook_events")).scalar_one()
    assert count == 0, "an unverified signature must never be stored, even as a rejected row"
    sync_engine.dispose()


@pytest.mark.asyncio
async def test_valid_unresolving_event_is_stored_but_not_reconciled(
    async_client, migrated_db, monkeypatch
):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    body = json.dumps(
        {"event": "refund.processed", "payload": {"refund": {"entity": {"id": "rfnd_1"}}}}
    ).encode()
    resp = await async_client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"X-Razorpay-Signature": _sign(body), "Content-Type": "application/json"},
    )
    get_settings.cache_clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "stored"
    assert data["reconciled"] is False


@pytest.mark.asyncio
async def test_duplicate_delivery_is_idempotent(async_client, migrated_db, monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    body = json.dumps({"event": "payment.failed", "payload": {}, "nonce": str(uuid.uuid4())}).encode()
    headers = {"X-Razorpay-Signature": _sign(body), "Content-Type": "application/json"}

    resp1 = await async_client.post("/webhooks/razorpay", content=body, headers=headers)
    resp2 = await async_client.post("/webhooks/razorpay", content=body, headers=headers)
    get_settings.cache_clear()

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "already_processed"

    from sqlalchemy import create_engine

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM raw_webhook_events WHERE event_type = 'payment.failed'")
        ).scalar_one()
    assert count == 1, "an identical redelivery must not create a second row"
    sync_engine.dispose()


@pytest.mark.asyncio
async def test_real_webhook_reconciles_a_pending_recovery_to_a_real_ledger_row(
    async_client, migrated_db, monkeypatch
):
    """The actual bug this closes: RazorpayTestAdapter.retry() creates a
    real order, returns PENDING, and nothing used to ever resolve it. A
    real order.paid webhook now must produce a real terminal
    recovery_ledger row."""
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    order_id = f"order_{uuid.uuid4().hex[:14]}"
    payment_id = await _seed_pending_recovery(migrated_db, order_id=order_id, amount_paise=150000)

    body = json.dumps(
        {
            "event": "order.paid",
            "payload": {
                "order": {"entity": {"id": order_id, "amount_paid": 150000}},
                "payment": {"entity": {"id": "pay_abc123", "order_id": order_id, "amount": 150000}},
            },
        }
    ).encode()
    resp = await async_client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"X-Razorpay-Signature": _sign(body), "Content-Type": "application/json"},
    )
    get_settings.cache_clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["reconciled"] is True
    assert data["matched_recovery_id"] is not None

    from sqlalchemy import create_engine

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        recovery_row = conn.execute(
            text("SELECT outcome, recovered_amount_paise FROM recoveries WHERE payment_id = :pid"),
            {"pid": payment_id},
        ).mappings().first()
        ledger_row = conn.execute(
            text("SELECT actual_recovery_paise FROM recovery_ledger WHERE payment_id = :pid"),
            {"pid": payment_id},
        ).mappings().first()
        payment_status = conn.execute(
            text("SELECT status FROM payments WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
    sync_engine.dispose()

    assert recovery_row["outcome"] == "SUCCESS"
    assert recovery_row["recovered_amount_paise"] == 150000
    assert ledger_row is not None, "the PENDING-that-nothing-resolves bug: a real ledger row must now exist"
    assert ledger_row["actual_recovery_paise"] == 150000
    assert payment_status == "recovered"


@pytest.mark.asyncio
async def test_webhook_for_unknown_order_id_is_stored_but_not_reconciled(
    async_client, migrated_db, monkeypatch
):
    """A webhook for an order RecoveryOS never created (or already
    resolved) must not crash or fabricate a match."""
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    body = json.dumps(
        {
            "event": "order.paid",
            "payload": {"order": {"entity": {"id": "order_never_seen_before", "amount_paid": 500}}},
        }
    ).encode()
    resp = await async_client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"X-Razorpay-Signature": _sign(body), "Content-Type": "application/json"},
    )
    get_settings.cache_clear()

    assert resp.status_code == 200
    assert resp.json()["reconciled"] is False
    assert resp.json()["matched_recovery_id"] is None
