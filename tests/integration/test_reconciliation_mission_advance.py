"""
Phase 12/13 correctness fix -- services/pipeline/reconciliation.py's
reconcile_pending_recovery() must advance a payment's active RecoveryMission,
not just its recoveries/recovery_ledger rows, since for a real (non-
simulator) provider this webhook path is the ONLY place a PENDING attempt's
true terminal outcome is ever reported (workers/execution_worker.py's own
action_fn already returned once it recorded PENDING).

Fixture mirrors tests/integration/test_razorpay_webhook_endpoint.py's own
_seed_pending_recovery exactly (a real payment/candidate/decision/PENDING-
recovery chain), plus a recovery_missions row in EXECUTING state -- the
state services/pipeline/consumer.py would have left it in right before
enqueueing the (still-pending) job.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import seed_merchant_and_customer, to_async_url


async def _seed_pending_recovery_with_mission(
    migrated_db: str,
    *,
    order_id: str,
    amount_paise: int = 100_000,
    max_retries: int = 3,
    attempt_number: int = 1,
) -> tuple[str, str, str]:
    """Returns (payment_id, decision_id, mission_id)."""
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    engine = create_async_engine(to_async_url(migrated_db))
    payment_id = str(uuid.uuid4())
    candidate_id = str(uuid.uuid4())
    decision_id = str(uuid.uuid4())
    policy_config_id = str(uuid.uuid4())
    recovery_id = str(uuid.uuid4())
    mission_id = str(uuid.uuid4())
    now = datetime.now(UTC)
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
                "ts": now - timedelta(hours=1),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO policy_configs (policy_config_id, max_retries) VALUES (:pcid, :mr)"
            ),
            {"pcid": policy_config_id, "mr": max_retries},
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
                "VALUES (:rid, :pid, :did, :ik, :attempt, 'RETRY_NOW', now(), NULL, 'PENDING', 0, "
                ":oid, now())"
            ),
            {
                "rid": recovery_id,
                "pid": payment_id,
                "did": decision_id,
                "ik": f"recovery:{payment_id}:RETRY_NOW:{attempt_number}",
                "attempt": attempt_number,
                "oid": order_id,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO recovery_missions (mission_id, payment_id, state, objective, "
                "max_investigation_rounds, max_attempts, max_mission_duration_seconds, "
                "max_money_exposure_paise, current_round, current_attempt, started_at, expires_at) "
                # OBSERVING_OUTCOME, not EXECUTING -- matches reality: by the
                # time a PENDING recovery exists for reconciliation to match
                # against, workers/execution_worker.py's own PENDING-outcome
                # handling has already transitioned the mission there (see
                # services/pipeline/reconciliation.py's
                # _advance_mission_on_external_resolution docstring).
                "VALUES (:mid, :pid, 'OBSERVING_OUTCOME', 'test objective', 3, 3, 604800, :amount, "
                "0, :attempt, :now, :expires)"
            ),
            {
                "mid": mission_id,
                "pid": payment_id,
                "amount": amount_paise,
                # current_attempt reflects what happened BEFORE this
                # reconciliation call resolves attempt_number -- the PENDING
                # attempt itself already ran (execution_worker.py's own
                # OUTCOME_PENDING branch increments current_attempt for it),
                # so current_attempt == attempt_number here, matching what
                # a real PENDING resolution would have left behind.
                "attempt": attempt_number,
                "now": now,
                "expires": now + timedelta(days=7),
            },
        )
    await engine.dispose()
    return payment_id, decision_id, mission_id


async def _mission_row(migrated_db: str, mission_id: str) -> dict:
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT state, current_attempt FROM recovery_missions WHERE mission_id = :mid"
                    ),
                    {"mid": mission_id},
                )
            )
            .mappings()
            .first()
        )
    await engine.dispose()
    return dict(row)


async def _mission_events(migrated_db: str, mission_id: str) -> list[tuple]:
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT event_type, actor, state FROM mission_events "
                    "WHERE mission_id = :mid ORDER BY sequence_number"
                ),
                {"mid": mission_id},
            )
        ).fetchall()
    await engine.dispose()
    return [tuple(r) for r in rows]


@pytest.mark.asyncio
async def test_external_success_resolution_closes_an_executing_mission(migrated_db):
    from recoveryos.database import get_app_session_factory
    from services.pipeline.reconciliation import reconcile_pending_recovery

    order_id = f"order_{uuid.uuid4().hex[:12]}"
    payment_id, decision_id, mission_id = await _seed_pending_recovery_with_mission(
        migrated_db, order_id=order_id
    )

    async with get_app_session_factory()() as session:
        recovery_id = await reconcile_pending_recovery(
            session, order_id=order_id, outcome="SUCCESS", recovered_amount_paise=100_000
        )
    assert recovery_id is not None

    mission = await _mission_row(migrated_db, mission_id)
    assert mission["state"] == "RECOVERED"
    assert mission["current_attempt"] == 1

    events = await _mission_events(migrated_db, mission_id)
    event_types = [e[0] for e in events]
    assert "EXTERNAL_RESOLUTION" in event_types
    assert events[-1][0] == "MISSION_RECOVERED"


@pytest.mark.asyncio
async def test_external_failure_with_budget_remaining_reschedules_not_terminates(migrated_db):
    """FAILED with attempt_number=1 < max_retries=3 -- budget remains, so
    the mission must schedule a re-evaluation (Phase 13's closed loop) and
    stay in OBSERVING_OUTCOME, not TERMINATED."""
    from recoveryos.database import get_app_session_factory
    from services.pipeline.reconciliation import reconcile_pending_recovery

    order_id = f"order_{uuid.uuid4().hex[:12]}"
    payment_id, decision_id, mission_id = await _seed_pending_recovery_with_mission(
        migrated_db, order_id=order_id, max_retries=3, attempt_number=1
    )

    async with get_app_session_factory()() as session:
        recovery_id = await reconcile_pending_recovery(
            session, order_id=order_id, outcome="FAILED", recovered_amount_paise=0
        )
    assert recovery_id is not None

    mission = await _mission_row(migrated_db, mission_id)
    assert mission["state"] == "OBSERVING_OUTCOME"
    assert mission["current_attempt"] == 1

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        reeval = (
            (
                await conn.execute(
                    text(
                        "SELECT status, mission_id FROM scheduled_reevaluations WHERE payment_id = :pid"
                    ),
                    {"pid": payment_id},
                )
            )
            .mappings()
            .first()
        )
    await engine.dispose()
    assert reeval is not None
    assert reeval["status"] == "PENDING"
    assert str(reeval["mission_id"]) == mission_id


@pytest.mark.asyncio
async def test_external_failure_at_retry_limit_terminates_the_mission(migrated_db):
    """attempt_number == max_retries -- the deterministic stopping rule
    fires; the mission must TERMINATE, not schedule a reschedule that would
    only be blocked by RetryLimitRule on the next cycle anyway."""
    from recoveryos.database import get_app_session_factory
    from services.pipeline.reconciliation import reconcile_pending_recovery

    order_id = f"order_{uuid.uuid4().hex[:12]}"
    payment_id, decision_id, mission_id = await _seed_pending_recovery_with_mission(
        migrated_db, order_id=order_id, max_retries=1, attempt_number=1
    )

    async with get_app_session_factory()() as session:
        recovery_id = await reconcile_pending_recovery(
            session, order_id=order_id, outcome="FAILED", recovered_amount_paise=0
        )
    assert recovery_id is not None

    mission = await _mission_row(migrated_db, mission_id)
    assert mission["state"] == "TERMINATED"

    events = await _mission_events(migrated_db, mission_id)
    assert events[-1][0] == "STOPPING_RULE_TRIGGERED"


@pytest.mark.asyncio
async def test_reconciliation_without_an_active_mission_is_a_safe_no_op(migrated_db):
    """A payment with no recovery_missions row at all (e.g. a pre-Phase-12
    payment, or a direct/test invocation) must not raise -- reconciliation's
    core ledger/audit behavior is unaffected either way."""
    from recoveryos.database import get_app_session_factory
    from services.pipeline.reconciliation import reconcile_pending_recovery

    order_id = f"order_{uuid.uuid4().hex[:12]}"
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
                "VALUES (:pid, :mid, :cid, 100000, 'upi', 'HDFC', 'failed', 'TIMEOUT', 'TEMPORARY', "
                "true, now(), now())"
            ),
            {"pid": payment_id, "mid": merchant_id, "cid": customer_id},
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


    async with get_app_session_factory()() as session:
        recovery_id_result = await reconcile_pending_recovery(
            session, order_id=order_id, outcome="SUCCESS", recovered_amount_paise=100_000
        )
    assert str(recovery_id_result) == recovery_id
