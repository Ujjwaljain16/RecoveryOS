"""
Production Architecture Domain Audit, Finding #1 -- the Control Tower's
headline revenue/recovery-rate tiles used to read raw Prometheus Counters,
which reset to 0 in-process on any worker restart with zero reconciliation
against the durable recovery_ledger table. Fixed by computing
*_reconciled Gauges fresh from a live SQL aggregate on every /metrics
scrape (apps/api/routers/health.py), never accumulated in-process.

The mandatory proof: simulate a worker restart by resetting the RAW
Counter directly (the exact in-process state loss a real restart causes),
then confirm the RECONCILED gauge is completely unaffected -- it's
recomputed from the database every time, so there is no accumulated
state to lose.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import seed_merchant_and_customer, to_async_url


def _extract_gauge_value(metrics_text: str, series_name: str) -> float | None:
    for line in metrics_text.splitlines():
        if line.startswith(f"{series_name} "):
            return float(line.split(" ")[1])
    return None


async def _seed_ledger_row(
    migrated_db: str,
    *,
    revenue_at_risk_paise: int,
    actual_recovery_paise: int,
    incremental_recovery_paise: int,
) -> str:
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    payment_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, :amount, 'upi', 'HDFC', 'failed', 'TIMEOUT', "
                "'TEMPORARY', true, :ts, :ts)"
            ),
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "amount": revenue_at_risk_paise,
                "ts": datetime.now(UTC) - timedelta(hours=1),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO recovery_ledger (ledger_id, payment_id, revenue_at_risk_paise, "
                "expected_recovery_paise, actual_recovery_paise, incremental_recovery_paise, "
                "intervention_cost_paise, net_recovery_paise) "
                "VALUES (:lid, :pid, :risk, 0, :actual, :incremental, 0, :actual)"
            ),
            {
                "lid": str(uuid.uuid4()),
                "pid": payment_id,
                "risk": revenue_at_risk_paise,
                "actual": actual_recovery_paise,
                "incremental": incremental_recovery_paise,
            },
        )
    await engine.dispose()
    return payment_id


@pytest.mark.asyncio
async def test_reconciled_gauges_match_the_real_ledger_sum(async_client, migrated_db):
    await _seed_ledger_row(
        migrated_db,
        revenue_at_risk_paise=500_000,
        actual_recovery_paise=300_000,
        incremental_recovery_paise=50_000,
    )

    resp = await async_client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.connect() as conn:
        expected = (
            await conn.execute(
                text(
                    "SELECT SUM(revenue_at_risk_paise), SUM(actual_recovery_paise), "
                    "SUM(incremental_recovery_paise) FROM recovery_ledger"
                )
            )
        ).first()
    await engine.dispose()

    assert _extract_gauge_value(body, "revenue_at_risk_paise_reconciled") == float(expected[0])
    assert _extract_gauge_value(body, "revenue_recovered_paise_reconciled") == float(expected[1])
    assert _extract_gauge_value(body, "incremental_revenue_paise_reconciled") == float(expected[2])


@pytest.mark.asyncio
async def test_reconciled_gauge_survives_a_simulated_worker_restart(async_client, migrated_db):
    """
    THE mandatory regression test: reset the RAW Counter directly (exactly
    what happens to in-process state on a real container restart -- all
    accumulated value is gone), then confirm the RECONCILED gauge is
    completely correct anyway on the very next scrape, because it never
    depended on that accumulated state in the first place.
    """
    await _seed_ledger_row(
        migrated_db,
        revenue_at_risk_paise=750_000,
        actual_recovery_paise=600_000,
        incremental_recovery_paise=120_000,
    )

    from recoveryos.metrics import revenue_at_risk_paise_total

    # Simulate the worker restart: the raw Counter's in-process value is
    # wiped (a fresh process starts every Counter at 0 -- setting it
    # directly here is the test-equivalent of "the process just restarted").
    revenue_at_risk_paise_total._value.set(0)
    assert (
        revenue_at_risk_paise_total._value.get() == 0.0
    ), "test setup: the raw counter must genuinely be reset"

    resp = await async_client.get("/metrics")
    body = resp.text

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.connect() as conn:
        expected_at_risk = (
            await conn.execute(text("SELECT SUM(revenue_at_risk_paise) FROM recovery_ledger"))
        ).scalar_one()
    await engine.dispose()

    reconciled_value = _extract_gauge_value(body, "revenue_at_risk_paise_reconciled")
    assert reconciled_value == float(expected_at_risk), (
        f"the reconciled gauge must reflect the real ledger sum ({expected_at_risk}) regardless of "
        f"the raw counter being reset to 0 -- got {reconciled_value}"
    )
    # And prove the raw counter really did stay at its reset value -- this
    # test isn't accidentally reading the raw series under a different name.
    raw_value = _extract_gauge_value(body, "revenue_at_risk_paise_total")
    assert raw_value == 0.0, "sanity check: the raw counter must still show the simulated reset"


@pytest.mark.asyncio
async def test_reconciled_recovery_rate_gauges_match_the_recoveries_table(
    async_client, migrated_db
):
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    payment_id = str(uuid.uuid4())
    candidate_id = str(uuid.uuid4())
    decision_id = str(uuid.uuid4())
    policy_config_id = str(uuid.uuid4())

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, 100000, 'upi', 'HDFC', 'failed', 'TIMEOUT', "
                "'TEMPORARY', true, now(), now())"
            ),
            {"pid": payment_id, "mid": merchant_id, "cid": customer_id},
        )
        await conn.execute(
            text("INSERT INTO policy_configs (policy_config_id) VALUES (:id)"),
            {"id": policy_config_id},
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
                "attempt_number, action_type, scheduled_for, executed_at, outcome, recovered_amount_paise) "
                "VALUES (gen_random_uuid(), :pid, :did, :ik, 1, 'RETRY_NOW', now(), now(), 'SUCCESS', 100000)"
            ),
            {"pid": payment_id, "did": decision_id, "ik": f"recovery:{payment_id}:RETRY_NOW:1"},
        )
    await engine.dispose()

    resp = await async_client.get("/metrics")
    body = resp.text

    attempts = _extract_gauge_value(body, 'recovery_attempts_reconciled{action_type="RETRY_NOW"}')
    successes = _extract_gauge_value(body, 'recovery_success_reconciled{action_type="RETRY_NOW"}')
    assert attempts is not None and attempts >= 1.0
    assert successes is not None and successes >= 1.0
