"""
Production Architecture Domain Audit, Finding #6 -- `recoveries.
stopping_rule_triggered` (workers/execution_worker.py's
_compute_stopping_rule, its first-ever real writer) had ZERO test
coverage, and its boundary condition (`attempt_number >= max_retries`) is
maintained completely independently from services/policy_engine/rules.py's
RetryLimitRule (`attempt_number <= max_retries` allows, `>` blocks/
escalates) -- two hand-written numeric comparisons in two files, tied
together by nothing but a docstring's claim that they "imply" agreement.

This file has two parts:
  1. Direct tests of _compute_stopping_rule's own behavior (real Postgres --
     it queries policy_configs/policy_decisions for real).
  2. The cross-file consistency test the audit specifically asked for:
     for a range of attempt_number values against the SAME max_retries,
     prove RetryLimitRule's allow/block boundary and _compute_stopping_
     rule's trigger boundary stay complementary -- the LAST attempt_number
     RetryLimitRule allows must be EXACTLY the attempt_number
     _compute_stopping_rule marks as the stopping one. A future change to
     either file's boundary operator without updating the other would
     fail this test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text

from services.policy_engine.rules import (
    CandidateContext,
    PaymentContext,
    PolicyConfigContext,
    RetryLimitRule,
)
from tests.integration.conftest import seed_merchant_and_customer, to_async_url
from workers.execution_worker import (
    STOPPING_RULE_MAX_RETRIES,
    STOPPING_RULE_STOP_AFTER_SUCCESS,
    _compute_stopping_rule,
)


async def _seed_decision_with_policy_config(
    migrated_db: str, *, max_retries: int, stop_after_success: bool = True
) -> str:
    """A real policy_configs + candidate_actions + policy_decisions chain
    -- _compute_stopping_rule joins policy_decisions -> policy_configs for
    real, this can't be faked with in-memory dataclasses the way
    RetryLimitRule's own inputs can."""
    from sqlalchemy.ext.asyncio import create_async_engine

    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    payment_id = str(uuid.uuid4())
    policy_config_id = str(uuid.uuid4())
    candidate_id = str(uuid.uuid4())
    decision_id = str(uuid.uuid4())

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
            text(
                "INSERT INTO policy_configs (policy_config_id, max_retries, stop_after_success) "
                "VALUES (:id, :mr, :sas)"
            ),
            {"id": policy_config_id, "mr": max_retries, "sas": stop_after_success},
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
    await engine.dispose()
    return decision_id


@pytest.mark.asyncio
async def test_stop_after_success_fires_regardless_of_attempt_number(migrated_db):
    decision_id = await _seed_decision_with_policy_config(
        migrated_db, max_retries=5, stop_after_success=True
    )
    engine = create_engine(migrated_db, pool_pre_ping=True)
    with engine.connect() as conn:
        result = _compute_stopping_rule(conn, decision_id, attempt_number=1, outcome="SUCCESS")
    assert result == STOPPING_RULE_STOP_AFTER_SUCCESS
    engine.dispose()


@pytest.mark.asyncio
async def test_success_does_not_stop_when_stop_after_success_is_disabled(migrated_db):
    decision_id = await _seed_decision_with_policy_config(
        migrated_db, max_retries=5, stop_after_success=False
    )
    engine = create_engine(migrated_db, pool_pre_ping=True)
    with engine.connect() as conn:
        result = _compute_stopping_rule(conn, decision_id, attempt_number=1, outcome="SUCCESS")
    assert result is None
    engine.dispose()


@pytest.mark.asyncio
async def test_failed_below_max_retries_does_not_stop(migrated_db):
    decision_id = await _seed_decision_with_policy_config(migrated_db, max_retries=3)
    engine = create_engine(migrated_db, pool_pre_ping=True)
    with engine.connect() as conn:
        result = _compute_stopping_rule(conn, decision_id, attempt_number=2, outcome="FAILED")
    assert result is None
    engine.dispose()


@pytest.mark.asyncio
async def test_failed_exactly_at_max_retries_stops(migrated_db):
    decision_id = await _seed_decision_with_policy_config(migrated_db, max_retries=3)
    engine = create_engine(migrated_db, pool_pre_ping=True)
    with engine.connect() as conn:
        result = _compute_stopping_rule(conn, decision_id, attempt_number=3, outcome="FAILED")
    assert result == STOPPING_RULE_MAX_RETRIES
    engine.dispose()


@pytest.mark.asyncio
async def test_unresolvable_decision_id_returns_none_not_an_error(migrated_db):
    engine = create_engine(migrated_db, pool_pre_ping=True)
    with engine.connect() as conn:
        result = _compute_stopping_rule(conn, str(uuid.uuid4()), attempt_number=1, outcome="FAILED")
    assert result is None
    engine.dispose()


# ─── The cross-file consistency invariant ──────────────────────────────────


def _retry_limit_rule_allows(attempt_number: int, max_retries: int) -> bool:
    payment = PaymentContext(
        payment_id="p",
        status="failed",
        is_expired=False,
        opted_out_at=None,
        last_attempt_at=None,
        attempt_number=attempt_number,
        amount_paise=100_000,
        now=datetime(2026, 1, 1, tzinfo=UTC),
        method="upi",
        is_high_severity_anomaly=False,
    )
    candidate = CandidateContext(action_type="RETRY_NOW", expected_value_paise=1_000)
    policy_config = PolicyConfigContext(
        max_retries=max_retries,
        retry_cooldown_hours=12,
        max_amount_paise=2_500_000,
        escalate_after_failures=2,
        min_expected_value_paise=0,
    )
    return RetryLimitRule().check(payment, candidate, policy_config).passed


@pytest.mark.asyncio
@pytest.mark.parametrize("max_retries", [1, 2, 3, 5])
async def test_retry_limit_rule_and_stopping_rule_boundaries_stay_complementary(
    migrated_db, max_retries
):
    """
    THE test the audit asked for: for attempt_number in a range spanning
    the boundary, RetryLimitRule's allow/block decision and
    _compute_stopping_rule's trigger decision must agree on exactly where
    the sequence ends -- the LAST attempt_number RetryLimitRule allows
    must be the SAME attempt_number _compute_stopping_rule marks as
    MAX_RETRIES. If either file's boundary operator (`<=`/`>` vs `>=`)
    ever drifted out of sync, this test would catch it immediately.
    """
    decision_id = await _seed_decision_with_policy_config(
        migrated_db, max_retries=max_retries, stop_after_success=False
    )
    engine = create_engine(migrated_db, pool_pre_ping=True)

    last_allowed_attempt = None
    first_stopped_attempt = None

    for attempt_number in range(1, max_retries + 3):
        policy_allows = _retry_limit_rule_allows(attempt_number, max_retries)
        with engine.connect() as conn:
            stopping_result = _compute_stopping_rule(
                conn, decision_id, attempt_number=attempt_number, outcome="FAILED"
            )
        stopping_fires = stopping_result == STOPPING_RULE_MAX_RETRIES

        if policy_allows:
            last_allowed_attempt = attempt_number
        if stopping_fires and first_stopped_attempt is None:
            first_stopped_attempt = attempt_number

        # An attempt policy would BLOCK must never be silently un-marked --
        # once blocked, every later attempt_number must also both be
        # blocked AND report stopping_fires=True (>= is monotonic).
        if not policy_allows:
            assert stopping_fires, (
                f"attempt_number={attempt_number} (max_retries={max_retries}): RetryLimitRule "
                f"blocks this attempt but _compute_stopping_rule does not mark it stopped -- "
                f"a payment could be blocked by policy while recoveries.stopping_rule_triggered "
                f"stays NULL, an inconsistent audit trail"
            )

    engine.dispose()

    assert last_allowed_attempt == max_retries
    assert first_stopped_attempt == max_retries, (
        f"the LAST attempt RetryLimitRule allows ({last_allowed_attempt}) must be exactly the "
        f"attempt _compute_stopping_rule marks as MAX_RETRIES ({first_stopped_attempt}) -- "
        f"these are independently-maintained boundary conditions (RetryLimitRule's `<=`/`>` vs "
        f"_compute_stopping_rule's `>=`) that must stay complementary"
    )
