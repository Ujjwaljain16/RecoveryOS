"""
Adversarial sweep scenario #23 -- provider succeeds but the response is
duplicated/delayed.

Distinct from tests/integration/test_execution_worker.py's
test_duplicate_job_same_idempotency_key_executes_once (two callers racing to
INVOKE the provider) and test_idempotent_execution.py (generic idempotency
mechanism proof): this test asks a narrower question specific to the
PaymentProvider boundary -- if the provider itself is invoked a second time
for an idempotency_key that ALREADY has a persisted successful outcome (the
real-world shape of "the provider's success response arrived twice, or a
delayed retry from our own side reached the provider redundantly"), does the
accounting layer (recoveries, recovery_ledger) end up with duplicate rows or
double-counted revenue? execute_with_idempotency's lock-before-check pattern
should short-circuit the second call before the provider is even reached
again for a completed key -- this test proves that end to end through
process_job, not just at the idempotency primitive.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text

from integrations.razorpay.adapter import ProviderResult
from tests.integration.conftest import seed_merchant_and_customer, to_async_url
from workers.execution_worker import process_job


class DuplicateSuccessProvider:
    """A provider stub whose retry() always reports SUCCESS -- standing in
    for "the provider's underlying charge succeeded, and whatever channel
    reports that success back to us delivered it more than once." Records
    every real invocation so the test can assert how many times process_job
    actually reached the provider."""

    def __init__(self):
        self.call_count = 0

    def retry(self, conn, payment_id: str, amount_paise: int, attempt_number: int) -> ProviderResult:
        self.call_count += 1
        return ProviderResult(
            outcome="SUCCESS",
            provider_ref=f"dup_{uuid.uuid4().hex[:8]}",
            recovered_amount_paise=amount_paise,
        )


async def _seed_failed_payment(migrated_db: str, amount_paise: int = 150_000) -> str:
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    from sqlalchemy.ext.asyncio import create_async_engine

    payment_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, :amount, 'upi', 'HDFC', 'failed', 'TIMEOUT', true, now(), now())"
            ),
            {"pid": payment_id, "mid": merchant_id, "cid": customer_id, "amount": amount_paise},
        )
    await engine.dispose()
    return payment_id


async def _seed_decision_fk_chain(migrated_db: str, payment_id: str, amount_paise: int) -> str:
    from sqlalchemy.ext.asyncio import create_async_engine

    policy_config_id = str(uuid.uuid4())
    candidate_id = str(uuid.uuid4())
    decision_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO policy_configs (policy_config_id) VALUES (:id)"),
            {"id": policy_config_id},
        )
        await conn.execute(
            text(
                "INSERT INTO candidate_actions (candidate_id, payment_id, action_type, "
                "recovery_prob_bps, expected_value_paise, model_version) "
                "VALUES (:cid, :pid, 'RETRY_NOW', 8000, 1000, 'test')"
            ),
            {"cid": candidate_id, "pid": payment_id},
        )
        await conn.execute(
            text(
                "INSERT INTO policy_decisions (decision_id, payment_id, candidate_id, "
                "policy_config_id, verdict, rule_trace) "
                "VALUES (:did, :pid, :cid, :pcid, 'ALLOW', '[]'::jsonb)"
            ),
            {"did": decision_id, "pid": payment_id, "cid": candidate_id, "pcid": policy_config_id},
        )
    await engine.dispose()
    return decision_id


def _make_job(payment_id: str, decision_id: str, amount_paise: int, attempt_number: int = 1) -> dict:
    return {
        "payment_id": payment_id,
        "idempotency_key": f"recovery:{payment_id}:RETRY_NOW:{attempt_number}",
        "action_type": "RETRY_NOW",
        "attempt_number": attempt_number,
        "decision_id": decision_id,
        "amount_paise": amount_paise,
    }


@pytest.mark.asyncio
async def test_provider_reinvoked_after_already_recorded_success_does_not_double_count(migrated_db):
    """
    First call: process_job runs for real, provider reports SUCCESS, a
    recoveries row + ledger credit is persisted. Second call: the SAME job
    (same idempotency_key) is processed again -- simulating a delayed/
    duplicated success signal reaching our side a second time. The provider
    must not be re-invoked for real money movement, and accounting must not
    double-count.
    """
    amount_paise = 150_000
    payment_id = await _seed_failed_payment(migrated_db, amount_paise)
    decision_id = await _seed_decision_fk_chain(migrated_db, payment_id, amount_paise)
    job = _make_job(payment_id, decision_id, amount_paise)

    engine = create_engine(migrated_db, pool_pre_ping=True)
    spy = DuplicateSuccessProvider()

    with engine.connect() as conn:
        first = process_job(conn, job, provider=spy)
    with engine.connect() as conn:
        second = process_job(conn, job, provider=spy)

    assert spy.call_count == 1, (
        f"the provider must be invoked exactly once for a job whose idempotency_key "
        f"already has a persisted outcome -- it was invoked {spy.call_count} times"
    )
    assert first == second, "the redelivered call must return the SAME persisted result, not a fresh one"

    with engine.connect() as conn:
        recoveries_count = conn.execute(
            text("SELECT count(*) FROM recoveries WHERE idempotency_key = :key"),
            {"key": job["idempotency_key"]},
        ).scalar_one()
        ledger_total = conn.execute(
            text("SELECT COALESCE(SUM(actual_recovery_paise), 0) FROM recovery_ledger WHERE payment_id = :pid"),
            {"pid": payment_id},
        ).scalar_one()

    assert recoveries_count == 1, f"exactly one recoveries row expected, got {recoveries_count}"
    assert ledger_total == amount_paise, (
        f"ledger must credit the recovered amount exactly once ({amount_paise}), got {ledger_total} "
        "-- a duplicated provider success must never be double-counted"
    )

    engine.dispose()
