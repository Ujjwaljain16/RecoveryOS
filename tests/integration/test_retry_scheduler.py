"""
Task REPLAN1 — the continuous-replanning cycle, real Postgres + real Redis,
zero mocks: RETRY_LATER decision -> scheduled_reevaluations row with a real
future scheduled_for -> (clock pinned forward) retry_scheduler fires it ->
the FULL decision pipeline genuinely re-runs for that payment.

Forcing RETRY_LATER deterministically reuses the exact negative-control
fixture from test_next_best_action_negative_control.py: a fresh
high-severity anomaly_windows row + RETRY_NOW/RETRY_LATER action_costs
forced equal + every other action made deliberately uneconomical, so
RETRY_LATER wins purely on timing.py's real probability-penalty bypass, the
same mechanism the live pipeline actually uses in production.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import recoveryos.clock as clock_module
from services.recovery_engine.orchestrator import decide_and_persist
from services.recovery_engine.scheduling import (
    claim_reevaluation,
    fetch_due_reevaluations,
    schedule_reevaluation,
)
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


async def _seed_high_anomaly_retry_later_fixture(migrated_db: str) -> tuple[str, str]:
    """Real failed payment for a bank with a genuinely fresh high-severity
    anomaly window, plus action_costs forcing RETRY_LATER to be the
    economically correct choice -- same recipe as the existing negative
    control test, just wired through the full live pipeline instead of
    calling generate_candidate_actions/select_next_best_action directly."""
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    bank = f"REPLAN_BANK_{uuid.uuid4().hex[:8]}"
    payment_id = str(uuid.uuid4())

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        for action_type in ("RETRY_NOW", "RETRY_LATER"):
            await conn.execute(
                text(
                    "INSERT INTO action_costs (merchant_id, action_type, cost_paise, friction_base_paise) "
                    "VALUES (:mid, :action_type, 100, 10)"
                ),
                {"mid": merchant_id, "action_type": action_type},
            )
        for action_type in ("ALT_ROUTE", "REMINDER", "ESCALATE"):
            await conn.execute(
                text(
                    "INSERT INTO action_costs (merchant_id, action_type, cost_paise, friction_base_paise) "
                    "VALUES (:mid, :action_type, 10000000, 0)"
                ),
                {"mid": merchant_id, "action_type": action_type},
            )
        await conn.execute(
            text(
                "INSERT INTO anomaly_windows "
                "(window_id, scope_type, scope_entity, time_bucket, baseline_rate, "
                " observed_rate, z_score, severity, is_anomaly) "
                "VALUES (gen_random_uuid(), 'bank', :bank, :tb, 0.03, 0.30, 9.0, 'high', true)"
            ),
            {"bank": bank, "tb": datetime.now(UTC)},
        )
        await conn.execute(
            text(
                "INSERT INTO payments "
                "(payment_id, merchant_id, customer_id, amount_paise, method, bank, "
                " status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, 500000, 'upi', :bank, 'failed', 'TIMEOUT', "
                " 'TEMPORARY', true, :ts, :ts)"
            ),
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "bank": bank,
                "ts": datetime.now(UTC) - timedelta(hours=1),
            },
        )
    await engine.dispose()
    return payment_id, merchant_id


async def _insert_minimal_decision(migrated_db: str, payment_id: str) -> str:
    """A real policy_decisions row (which needs a candidate_id FK) for tests
    that only exercise services/recovery_engine/scheduling.py directly and
    don't go through the full decide_and_persist pipeline."""
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO policy_configs (policy_config_id) "
                "VALUES ('00000000-0000-0000-0000-000000000001') "
                "ON CONFLICT (policy_config_id) DO NOTHING"
            )
        )
        candidate_id = (
            await conn.execute(
                text(
                    "INSERT INTO candidate_actions "
                    "(candidate_id, payment_id, action_type, recovery_prob_bps, "
                    " expected_value_paise, cost_paise, friction_penalty_paise, "
                    " risk_penalty_paise, model_version) "
                    "VALUES (gen_random_uuid(), :pid, 'RETRY_LATER', 8000, 400000, 100, 10, 0, "
                    "'test-v1') RETURNING candidate_id"
                ),
                {"pid": payment_id},
            )
        ).scalar_one()
        decision_id = (
            await conn.execute(
                text(
                    "INSERT INTO policy_decisions "
                    "(decision_id, payment_id, candidate_id, policy_config_id, verdict, rule_trace) "
                    "VALUES (gen_random_uuid(), :pid, :cid, "
                    "'00000000-0000-0000-0000-000000000001', 'ALLOW', '[]'::jsonb) "
                    "RETURNING decision_id"
                ),
                {"pid": payment_id, "cid": candidate_id},
            )
        ).scalar_one()
    await engine.dispose()
    return str(decision_id)


@pytest.mark.asyncio
async def test_retry_later_schedules_a_real_future_reevaluation_instead_of_executing(
    migrated_db, redis_client
):
    payment_id, _ = await _seed_high_anomaly_retry_later_fixture(migrated_db)

    result = await decide_and_persist(payment_id, redis_client=redis_client)

    print(f"\n[REPLAN1] decision for payment_id={payment_id}: {result}")
    assert result["verdict"] == "ALLOW"
    assert result["chosen_action"] == "RETRY_LATER"
    assert "scheduled_reevaluation_id" in result, (
        "RETRY_LATER must defer via scheduled_reevaluations, not enqueue an "
        "immediate execution job"
    )

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        recoveries_count = (
            await conn.execute(
                text("SELECT count(*) FROM recoveries WHERE payment_id = :pid"), {"pid": payment_id}
            )
        ).scalar_one()
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT status, scheduled_for FROM scheduled_reevaluations "
                        "WHERE reevaluation_id = :rid"
                    ),
                    {"rid": result["scheduled_reevaluation_id"]},
                )
            )
            .mappings()
            .first()
        )
    await engine.dispose()

    assert recoveries_count == 0, "RETRY_LATER must not create an executed recovery attempt"
    assert row is not None
    assert row["status"] == "PENDING"
    # High-severity anomaly -> the short 30-minute re-evaluation window, not
    # the merchant's full 12-hour default cooldown. Compared against the
    # session-wide PINNED clock (tests/conftest.py), not real wall-clock
    # time -- orchestrator.py computed this using clock.utcnow() too.
    pinned_now = clock_module.utcnow()
    assert row["scheduled_for"] > pinned_now
    assert row["scheduled_for"] < pinned_now + timedelta(hours=1)


@pytest.mark.asyncio
async def test_retry_scheduler_fires_due_row_and_genuinely_reevaluates(migrated_db, redis_client):
    """
    Full cycle: defer -> time-travel (clock pin, not a sleep) -> scheduler
    fires -> the decision pipeline actually re-runs for this payment (a
    second, distinct diagnoses row appears; the fired row transitions to
    FIRED with a fresh fired_source_event_id) -- proving this is a genuine
    re-evaluation, not a stale replay of the original decision.
    """
    from workers.retry_scheduler import run_once

    payment_id, _ = await _seed_high_anomaly_retry_later_fixture(migrated_db)
    result = await decide_and_persist(payment_id, redis_client=redis_client)
    assert result["chosen_action"] == "RETRY_LATER"
    reevaluation_id = result["scheduled_reevaluation_id"]

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        diagnoses_before = (
            await conn.execute(
                text("SELECT count(*) FROM diagnoses WHERE payment_id = :pid"), {"pid": payment_id}
            )
        ).scalar_one()

    future_time = datetime.now(UTC) + timedelta(hours=1)
    original_utcnow = clock_module.utcnow
    clock_module.utcnow = lambda: future_time
    try:
        processed_count = await run_once(redis_client)
    finally:
        clock_module.utcnow = original_utcnow

    assert processed_count >= 1

    async with engine.begin() as conn:
        fired_row = (
            (
                await conn.execute(
                    text(
                        "SELECT status, fired_source_event_id FROM scheduled_reevaluations "
                        "WHERE reevaluation_id = :rid"
                    ),
                    {"rid": reevaluation_id},
                )
            )
            .mappings()
            .first()
        )
        diagnoses_after = (
            await conn.execute(
                text("SELECT count(*) FROM diagnoses WHERE payment_id = :pid"), {"pid": payment_id}
            )
        ).scalar_one()
    await engine.dispose()

    print(
        f"\n[REPLAN1 cycle] fired_row={dict(fired_row) if fired_row else None} "
        f"diagnoses before={diagnoses_before} after={diagnoses_after}"
    )
    assert fired_row is not None
    assert fired_row["status"] == "FIRED"
    assert fired_row["fired_source_event_id"] is not None
    assert (
        diagnoses_after > diagnoses_before
    ), "the scheduler must genuinely re-run the decision pipeline, not just flip a status flag"


@pytest.mark.asyncio
async def test_schedule_reevaluation_dedups_on_same_source_event(migrated_db):
    """Same S1 dedup discipline as diagnoses/candidate_actions/policy_decisions:
    a redelivered triggering event must not create a second schedule row."""
    payment_id, merchant_id = await _seed_high_anomaly_retry_later_fixture(migrated_db)
    source_event_id = str(uuid.uuid4())
    decision_id = await _insert_minimal_decision(migrated_db, payment_id)

    scheduled_for = datetime.now(UTC) + timedelta(minutes=30)
    first_id = await schedule_reevaluation(
        payment_id=payment_id,
        decision_id=decision_id,
        diagnosis_id=None,
        source_event_id=source_event_id,
        scheduled_for=scheduled_for,
    )
    second_id = await schedule_reevaluation(
        payment_id=payment_id,
        decision_id=decision_id,
        diagnosis_id=None,
        source_event_id=source_event_id,
        scheduled_for=scheduled_for,
    )

    assert first_id == second_id

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        count = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM scheduled_reevaluations "
                    "WHERE payment_id = :pid AND source_event_id = :sid"
                ),
                {"pid": payment_id, "sid": source_event_id},
            )
        ).scalar_one()
    await engine.dispose()
    assert count == 1


@pytest.mark.asyncio
async def test_claim_reevaluation_is_atomic_under_concurrent_claims(migrated_db):
    """Two 'scheduler instances' racing to claim the SAME row -- only one
    may win, matching the migration's own documented concurrency mechanism
    (WHERE status='PENDING', no advisory lock)."""
    from recoveryos.database import get_app_session_factory

    payment_id, merchant_id = await _seed_high_anomaly_retry_later_fixture(migrated_db)
    decision_id = await _insert_minimal_decision(migrated_db, payment_id)

    reevaluation_id = await schedule_reevaluation(
        payment_id=payment_id,
        decision_id=decision_id,
        diagnosis_id=None,
        source_event_id=str(uuid.uuid4()),
        scheduled_for=datetime.now(UTC),
    )

    session_factory = get_app_session_factory()
    now = datetime.now(UTC)
    async with session_factory() as session_a, session_factory() as session_b:
        won_a = await claim_reevaluation(session_a, reevaluation_id, str(uuid.uuid4()), now)
        won_b = await claim_reevaluation(session_b, reevaluation_id, str(uuid.uuid4()), now)

    assert won_a != won_b, "exactly one of the two concurrent claims must win"
    assert won_a or won_b


@pytest.mark.asyncio
async def test_fetch_due_reevaluations_only_returns_pending_rows_whose_time_has_come(migrated_db):
    payment_id, merchant_id = await _seed_high_anomaly_retry_later_fixture(migrated_db)
    decision_id = await _insert_minimal_decision(migrated_db, payment_id)

    due_id = await schedule_reevaluation(
        payment_id=payment_id,
        decision_id=decision_id,
        diagnosis_id=None,
        source_event_id=str(uuid.uuid4()),
        scheduled_for=datetime.now(UTC) - timedelta(minutes=5),
    )
    not_due_id = await schedule_reevaluation(
        payment_id=payment_id,
        decision_id=decision_id,
        diagnosis_id=None,
        source_event_id=str(uuid.uuid4()),
        scheduled_for=datetime.now(UTC) + timedelta(hours=6),
    )

    from recoveryos.database import get_app_session_factory

    async with get_app_session_factory()() as session:
        due_rows = await fetch_due_reevaluations(session, datetime.now(UTC))

    due_ids = {r["reevaluation_id"] for r in due_rows}
    assert due_id in due_ids
    assert not_due_id not in due_ids
