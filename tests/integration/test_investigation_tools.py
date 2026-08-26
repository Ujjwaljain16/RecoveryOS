"""
Task AGENT1, point 2 -- the investigation tool registry (services/
diagnosis_engine/tools.py). Every tool runs on a REAL diagnoser_role
connection (the actual 'diagnoser' login user, not a superuser SET ROLE),
same discipline as test_diagnosis_engine.py's role-boundary tests: if a
grant were missing, these would fail with a real permission-denied error,
not a mock returning whatever we told it to.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from recoveryos.database import get_diagnoser_session_factory
from services.diagnosis_engine.tools import (
    TOOL_REGISTRY,
    call_tool,
    get_cohort_failure_rate,
    get_customer_payment_history,
    get_customer_recovery_history,
    get_intervention_history,
    get_payment_attempt_history,
    get_recent_anomalies,
)
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


async def _seed(migrated_db: str, bank: str = "HDFC"):
    """
    bank is overridable and defaults to a shared value across most tests
    (fine, since they only ever query a specific customer_id/payment_id).
    Cohort-scoped tests, which count ALL payments for a bank+method
    regardless of which test created them (migrated_db is session-scoped --
    same accumulation issue as tests/integration/test_chaos_fault_injection.py),
    must pass a unique bank string to get an isolated count.
    """
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    engine = create_async_engine(to_async_url(migrated_db))
    payment_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        # 3 prior payments for this customer, plus the one under test
        for i in range(3):
            await conn.execute(
                text(
                    "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                    "method, bank, status, failure_code, is_synthetic, created_at, failed_at) "
                    "VALUES (:pid, :mid, :cid, 50000, 'upi', :bank, 'success', NULL, true, :ts, NULL)"
                ),
                {"pid": str(uuid.uuid4()), "mid": merchant_id, "cid": customer_id, "bank": bank,
                 "ts": datetime.now(UTC) - timedelta(days=i + 1)},
            )
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, 100000, 'upi', :bank, 'failed', 'TIMEOUT', true, now(), now())"
            ),
            {"pid": payment_id, "mid": merchant_id, "cid": customer_id, "bank": bank},
        )
        policy_config_id = str(uuid.uuid4())
        await conn.execute(
            text("INSERT INTO policy_configs (policy_config_id) VALUES (:pcid)"),
            {"pcid": policy_config_id},
        )

        # a candidate_action + policy_decision + recovery for this payment
        candidate_id = str(uuid.uuid4())
        await conn.execute(
            text(
                "INSERT INTO candidate_actions (candidate_id, payment_id, action_type, "
                "recovery_prob_bps, expected_value_paise, cost_paise, friction_penalty_paise, "
                "risk_penalty_paise, model_version, created_at) "
                "VALUES (:cid, :pid, 'RETRY_NOW', 8000, 80000, 0, 0, 0, 'test-v1', now())"
            ),
            {"cid": candidate_id, "pid": payment_id},
        )
        decision_id = str(uuid.uuid4())
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
                "recovered_amount_paise, created_at) "
                "VALUES (:rid, :pid, :did, :ik, 1, 'RETRY_NOW', now(), now(), 'FAILED', 0, now())"
            ),
            {"rid": str(uuid.uuid4()), "pid": payment_id, "did": decision_id,
             "ik": f"recovery:{payment_id}:RETRY_NOW:1"},
        )
    await engine.dispose()
    return payment_id, merchant_id, customer_id


@pytest.mark.asyncio
async def test_get_customer_payment_history_real_diagnoser_connection(migrated_db):
    payment_id, _, customer_id = await _seed(migrated_db)
    session_factory = get_diagnoser_session_factory()
    async with session_factory() as session:
        history = await get_customer_payment_history(session, customer_id)

    assert len(history) == 4  # 3 successes + the failed one under test
    assert all("payment_id" in row for row in history)
    assert any(row["status"] == "failed" for row in history)


@pytest.mark.asyncio
async def test_get_customer_recovery_history_real_diagnoser_connection(migrated_db):
    payment_id, _, customer_id = await _seed(migrated_db)
    session_factory = get_diagnoser_session_factory()
    async with session_factory() as session:
        history = await get_customer_recovery_history(session, customer_id)

    assert len(history) == 1
    assert history[0]["outcome"] == "FAILED"
    assert str(history[0]["payment_id"]) == payment_id


@pytest.mark.asyncio
async def test_get_cohort_failure_rate_real_diagnoser_connection(migrated_db):
    isolated_bank = f"TESTBANK_{uuid.uuid4().hex[:8]}"
    await _seed(migrated_db, bank=isolated_bank)
    session_factory = get_diagnoser_session_factory()
    async with session_factory() as session:
        result = await get_cohort_failure_rate(session, isolated_bank, "upi", window_minutes=10_000)

    assert result["bank"] == isolated_bank
    assert result["current_sample_size"] == 4
    assert result["current_failure_rate"] == pytest.approx(0.25)  # 1 failed of 4


@pytest.mark.asyncio
async def test_get_recent_anomalies_no_bank_returns_empty(migrated_db):
    session_factory = get_diagnoser_session_factory()
    async with session_factory() as session:
        result = await get_recent_anomalies(session, None, "upi")
    assert result == []


@pytest.mark.asyncio
async def test_get_payment_attempt_history_real_diagnoser_connection(migrated_db):
    payment_id, _, _ = await _seed(migrated_db)
    session_factory = get_diagnoser_session_factory()
    async with session_factory() as session:
        attempts = await get_payment_attempt_history(session, payment_id)

    assert len(attempts) == 1
    assert attempts[0]["attempt_number"] == 1
    assert attempts[0]["outcome"] == "FAILED"


@pytest.mark.asyncio
async def test_get_intervention_history_real_diagnoser_connection(migrated_db):
    payment_id, _, _ = await _seed(migrated_db)
    session_factory = get_diagnoser_session_factory()
    async with session_factory() as session:
        decisions = await get_intervention_history(session, payment_id)

    assert len(decisions) == 1
    assert decisions[0]["verdict"] == "ALLOW"


@pytest.mark.asyncio
async def test_call_tool_dispatches_by_registered_name(migrated_db):
    payment_id, _, _ = await _seed(migrated_db)
    session_factory = get_diagnoser_session_factory()
    async with session_factory() as session:
        result = await call_tool(session, "get_payment_attempt_history", payment_id=payment_id)
    assert len(result) == 1


@pytest.mark.asyncio
async def test_call_tool_rejects_unregistered_name(migrated_db):
    session_factory = get_diagnoser_session_factory()
    async with session_factory() as session:
        with pytest.raises(KeyError):
            await call_tool(session, "drop_all_tables", payment_id="x")


def test_every_registered_tool_has_a_real_implementation():
    from services.diagnosis_engine.tools import _TOOL_IMPLEMENTATIONS

    assert set(TOOL_REGISTRY.keys()) == set(_TOOL_IMPLEMENTATIONS.keys())


def test_tool_costs_and_latencies_are_positive_real_constants():
    for spec in TOOL_REGISTRY.values():
        assert spec.tool_cost > 0
        assert spec.latency_ms_estimate > 0
