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
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "amount": amount_paise,
                "ts": datetime.now(UTC) - timedelta(hours=1),
            },
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
            {
                "rid": recovery_id,
                "pid": payment_id,
                "did": decision_id,
                "ik": f"recovery:{payment_id}:RETRY_NOW:1",
                "oid": order_id,
            },
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

    body = json.dumps(
        {"event": "payment.failed", "payload": {}, "nonce": str(uuid.uuid4())}
    ).encode()
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
async def test_x_razorpay_event_id_header_is_the_real_dedup_key(
    async_client, migrated_db, monkeypatch
):
    """
    Domain Audit finding F4, proven through the REAL endpoint (not just
    the pure compute_idempotency_key unit tests) -- two deliveries sharing
    the same X-Razorpay-Event-Id must dedupe as ONE event even if their
    bodies differ (Razorpay re-serializing a redelivered payload), and two
    deliveries with DIFFERENT event ids must never dedupe even if their
    bodies happen to be identical.
    """
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    event_id = f"evt_{uuid.uuid4().hex[:12]}"
    body_v1 = json.dumps({"event": "refund.processed", "payload": {}}).encode()
    body_v2 = json.dumps({"payload": {}, "event": "refund.processed"}).encode()  # reserialized

    resp1 = await async_client.post(
        "/webhooks/razorpay",
        content=body_v1,
        headers={
            "X-Razorpay-Signature": _sign(body_v1),
            "X-Razorpay-Event-Id": event_id,
            "Content-Type": "application/json",
        },
    )
    resp2 = await async_client.post(
        "/webhooks/razorpay",
        content=body_v2,
        headers={
            "X-Razorpay-Signature": _sign(body_v2),
            "X-Razorpay-Event-Id": event_id,
            "Content-Type": "application/json",
        },
    )
    get_settings.cache_clear()

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert (
        resp2.json()["status"] == "already_processed"
    ), "same X-Razorpay-Event-Id with a differently-serialized body must still dedupe"

    from sqlalchemy import create_engine

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT idempotency_key FROM raw_webhook_events WHERE idempotency_key LIKE :pat"),
            {"pat": f"evtid:{event_id}%"},
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == f"evtid:{event_id}"
    sync_engine.dispose()


@pytest.mark.asyncio
async def test_byte_identical_bodies_with_different_event_ids_do_not_dedup(
    async_client, migrated_db, monkeypatch
):
    """The other direction content-hash-only dedup gets wrong: two
    genuinely distinct events (different X-Razorpay-Event-Id) whose bodies
    happen to be byte-identical must be stored as TWO separate rows."""
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    body = json.dumps(
        {"event": "refund.processed", "payload": {}, "tag": str(uuid.uuid4())}
    ).encode()

    resp1 = await async_client.post(
        "/webhooks/razorpay",
        content=body,
        headers={
            "X-Razorpay-Signature": _sign(body),
            "X-Razorpay-Event-Id": f"evt_{uuid.uuid4().hex[:12]}",
            "Content-Type": "application/json",
        },
    )
    resp2 = await async_client.post(
        "/webhooks/razorpay",
        content=body,
        headers={
            "X-Razorpay-Signature": _sign(body),
            "X-Razorpay-Event-Id": f"evt_{uuid.uuid4().hex[:12]}",
            "Content-Type": "application/json",
        },
    )
    get_settings.cache_clear()

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert (
        resp2.json()["status"] != "already_processed"
    ), "byte-identical bodies with DIFFERENT event ids must be treated as two distinct events"


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
        recovery_row = (
            conn.execute(
                text(
                    "SELECT outcome, recovered_amount_paise FROM recoveries WHERE payment_id = :pid"
                ),
                {"pid": payment_id},
            )
            .mappings()
            .first()
        )
        ledger_row = (
            conn.execute(
                text("SELECT actual_recovery_paise FROM recovery_ledger WHERE payment_id = :pid"),
                {"pid": payment_id},
            )
            .mappings()
            .first()
        )
        payment_status = conn.execute(
            text("SELECT status FROM payments WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
    sync_engine.dispose()

    assert recovery_row["outcome"] == "SUCCESS"
    assert recovery_row["recovered_amount_paise"] == 150000
    assert (
        ledger_row is not None
    ), "the PENDING-that-nothing-resolves bug: a real ledger row must now exist"
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


@pytest.mark.asyncio
async def test_a_later_success_on_the_same_order_upgrades_an_already_failed_recovery(
    async_client, migrated_db, monkeypatch
):
    """
    The real scenario found live-testing this integration: Razorpay lets a
    customer retry a DIFFERENT payment method on the SAME order after a
    decline (a card declined, then netbanking succeeds). The first
    (payment.failed) webhook correctly resolves the recovery to FAILED;
    the second (payment.captured) webhook for the SAME order_id must
    UPGRADE that recovery to SUCCESS -- not be silently dropped as "no
    PENDING recovery matched," which is what used to happen.
    """
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    order_id = f"order_{uuid.uuid4().hex[:14]}"
    payment_id = await _seed_pending_recovery(migrated_db, order_id=order_id, amount_paise=50000)

    failed_body = json.dumps(
        {
            "event": "payment.failed",
            "payload": {"payment": {"entity": {"id": "pay_declined1", "order_id": order_id}}},
        }
    ).encode()
    failed_resp = await async_client.post(
        "/webhooks/razorpay",
        content=failed_body,
        headers={"X-Razorpay-Signature": _sign(failed_body), "Content-Type": "application/json"},
    )
    assert failed_resp.status_code == 200
    assert failed_resp.json()["reconciled"] is True

    from sqlalchemy import create_engine

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        after_first = conn.execute(
            text("SELECT outcome FROM recoveries WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
    assert after_first == "FAILED", "test setup: the first webhook must genuinely resolve to FAILED"

    captured_body = json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_netbanking_success",
                        "order_id": order_id,
                        "amount": 50000,
                    }
                }
            },
        }
    ).encode()
    captured_resp = await async_client.post(
        "/webhooks/razorpay",
        content=captured_body,
        headers={"X-Razorpay-Signature": _sign(captured_body), "Content-Type": "application/json"},
    )
    get_settings.cache_clear()

    assert captured_resp.status_code == 200
    data = captured_resp.json()
    assert (
        data["reconciled"] is True
    ), "a real capture on the same order must upgrade the FAILED recovery"
    assert data["matched_recovery_id"] is not None

    with sync_engine.connect() as conn:
        recovery_row = (
            conn.execute(
                text(
                    "SELECT outcome, recovered_amount_paise FROM recoveries WHERE payment_id = :pid"
                ),
                {"pid": payment_id},
            )
            .mappings()
            .first()
        )
        ledger_row = (
            conn.execute(
                text("SELECT actual_recovery_paise FROM recovery_ledger WHERE payment_id = :pid"),
                {"pid": payment_id},
            )
            .mappings()
            .first()
        )
        payment_status = conn.execute(
            text("SELECT status FROM payments WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
        audit_count = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
    sync_engine.dispose()

    assert recovery_row["outcome"] == "SUCCESS"
    assert recovery_row["recovered_amount_paise"] == 50000
    assert (
        ledger_row["actual_recovery_paise"] == 50000
    ), "the ledger row must be REVISED, not left at 0"
    assert payment_status == "recovered"
    # audit_log is append-only -- the correction is a SECOND entry, not an
    # edit of the original FAILED-resolution entry.
    assert audit_count == 2


@pytest.mark.asyncio
async def test_a_later_failure_on_an_already_successful_recovery_never_downgrades_it(
    async_client, migrated_db, monkeypatch
):
    """Negative control for the upgrade path above: a real SUCCESS is
    unambiguous ground truth. A late/out-of-order FAILED webhook for the
    same order arriving after a genuine SUCCESS must never un-recover it."""
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    order_id = f"order_{uuid.uuid4().hex[:14]}"
    payment_id = await _seed_pending_recovery(migrated_db, order_id=order_id, amount_paise=75000)

    captured_body = json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {"entity": {"id": "pay_success1", "order_id": order_id, "amount": 75000}}
            },
        }
    ).encode()
    captured_resp = await async_client.post(
        "/webhooks/razorpay",
        content=captured_body,
        headers={"X-Razorpay-Signature": _sign(captured_body), "Content-Type": "application/json"},
    )
    assert captured_resp.status_code == 200
    assert captured_resp.json()["reconciled"] is True

    failed_body = json.dumps(
        {
            "event": "payment.failed",
            "payload": {"payment": {"entity": {"id": "pay_late_failed", "order_id": order_id}}},
        }
    ).encode()
    failed_resp = await async_client.post(
        "/webhooks/razorpay",
        content=failed_body,
        headers={"X-Razorpay-Signature": _sign(failed_body), "Content-Type": "application/json"},
    )
    get_settings.cache_clear()

    assert failed_resp.status_code == 200
    assert (
        failed_resp.json()["reconciled"] is False
    ), "a FAILED webhook must never match an already-SUCCESS recovery"

    from sqlalchemy import create_engine

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        recovery_row = (
            conn.execute(
                text(
                    "SELECT outcome, recovered_amount_paise FROM recoveries WHERE payment_id = :pid"
                ),
                {"pid": payment_id},
            )
            .mappings()
            .first()
        )
        ledger_row = (
            conn.execute(
                text("SELECT actual_recovery_paise FROM recovery_ledger WHERE payment_id = :pid"),
                {"pid": payment_id},
            )
            .mappings()
            .first()
        )
    sync_engine.dispose()

    assert recovery_row["outcome"] == "SUCCESS", "a real capture must never be downgraded"
    assert recovery_row["recovered_amount_paise"] == 75000
    assert ledger_row["actual_recovery_paise"] == 75000
