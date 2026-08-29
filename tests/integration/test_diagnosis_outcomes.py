"""
Task AGENT1, agent-design review point 4 -- closing the diagnosis-to-outcome
loop. services/pipeline/ledger.py now writes a diagnosis_outcomes row
(migration 0015) at every terminal ledger write, real Postgres, real
app_role connection.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from services.pipeline.consumer import process_payment_failure
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


async def _seed_payment_with_ground_truth(
    migrated_db: str,
    *,
    true_recovery_prob_bps: int,
    true_failure_type: str,
    opted_out: bool = False,
) -> tuple[str, str]:
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    engine = create_async_engine(to_async_url(migrated_db))
    payment_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        if opted_out:
            await conn.execute(
                text("UPDATE customers SET opted_out_at = :ts WHERE customer_id = :cid"),
                {"ts": datetime.now(UTC) - timedelta(days=1), "cid": customer_id},
            )
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, 200000, 'upi', 'HDFC', 'failed', 'TIMEOUT', 'TEMPORARY', "
                "true, :ts, :ts)"
            ),
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "ts": datetime.now(UTC) - timedelta(hours=1),
            },
        )
        sim_id = str(uuid.uuid4())
        await conn.execute(
            text(
                "INSERT INTO simulator_manifests (simulation_id, seed, generator_version, "
                "scenario_config, latent_function_version, total_payments) "
                "VALUES (:sim_id, 1, 'test', '{}'::jsonb, 'test-v1', 1)"
            ),
            {"sim_id": sim_id},
        )
        await conn.execute(
            text(
                "INSERT INTO simulator_latent_state (latent_id, simulation_id, payment_id, "
                "customer_patience_score, bank_latent_health, latent_network_noise, "
                "latent_customer_propensity, true_recovery_prob_bps, true_failure_type) "
                "VALUES (:lid, :sim_id, :pid, 0.8, 0.9, 0.1, 0.2, :prob, :tft)"
            ),
            {
                "lid": str(uuid.uuid4()),
                "sim_id": sim_id,
                "pid": payment_id,
                "prob": true_recovery_prob_bps,
                "tft": true_failure_type,
            },
        )
    await engine.dispose()
    return payment_id, customer_id


def _run_execution_worker_once(migrated_db: str) -> None:
    import redis as sync_redis

    from recoveryos.config import get_settings
    from workers.execution_worker import run_worker

    settings = get_settings()
    sync_client = sync_redis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    run_worker(sync_client, max_iterations=1)
    sync_client.close()


@pytest.mark.asyncio
async def test_successful_recovery_records_effective_and_correct_outcome(migrated_db, redis_client):
    """TEMPORARY_GATEWAY_TIMEOUT ground truth + a deterministic-fallback
    diagnosis (no LLM configured in the test env, per tests/conftest.py's
    session-wide override) should classify as temporary_bank_degradation --
    correct -- and a guaranteed-recovery probability should make the
    execution succeed, so action_effective must be True."""
    payment_id, _ = await _seed_payment_with_ground_truth(
        migrated_db, true_recovery_prob_bps=10000, true_failure_type="TEMPORARY_GATEWAY_TIMEOUT"
    )
    await process_payment_failure(payment_id, "HDFC", redis_client)

    sync_engine_url = migrated_db
    from sqlalchemy import create_engine

    sync_engine = create_engine(sync_engine_url, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        pending_job = conn.execute(
            text("SELECT count(*) FROM recoveries WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
    if pending_job == 0:
        _run_execution_worker_once(migrated_db)

    with sync_engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT o.chosen_action, o.observed_outcome, o.diagnosis_correct, "
                    "o.action_effective, o.counterfactual_result "
                    "FROM diagnosis_outcomes o JOIN diagnoses d ON d.diagnosis_id = o.diagnosis_id "
                    "WHERE d.payment_id = :pid"
                ),
                {"pid": payment_id},
            )
            .mappings()
            .first()
        )

    assert row is not None, "diagnosis_outcomes row must exist after a terminal ledger write"
    assert row["observed_outcome"] == "SUCCESS"
    assert row["action_effective"] is True
    assert (
        row["diagnosis_correct"] is True
    )  # fallback correctly maps TIMEOUT -> temporary_bank_degradation
    assert row["counterfactual_result"]["actual_recovery_paise"] == 200000
    sync_engine.dispose()


@pytest.mark.asyncio
async def test_blocked_payment_records_outcome_with_null_action_effective(
    migrated_db, redis_client
):
    """An opted-out customer -> BLOCK, no execution ever attempted --
    action_effective must be NULL (not applicable), not False."""
    payment_id, _ = await _seed_payment_with_ground_truth(
        migrated_db,
        true_recovery_prob_bps=5000,
        true_failure_type="CUSTOMER_INSUFFICIENT_FUNDS",
        opted_out=True,
    )
    await process_payment_failure(payment_id, "HDFC", redis_client)

    from sqlalchemy import create_engine

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT o.observed_outcome, o.action_effective FROM diagnosis_outcomes o "
                    "JOIN diagnoses d ON d.diagnosis_id = o.diagnosis_id WHERE d.payment_id = :pid"
                ),
                {"pid": payment_id},
            )
            .mappings()
            .first()
        )

    assert row is not None
    assert row["observed_outcome"] == "BLOCK"
    assert row["action_effective"] is None
    sync_engine.dispose()


@pytest.mark.asyncio
async def test_redelivery_does_not_duplicate_outcome_row(migrated_db, redis_client):
    """Same S1 dedup discipline extended to diagnosis_outcomes: a
    redelivered decision for a payment already at a terminal ledger state
    must not create a second outcome row (unique constraint on diagnosis_id)."""
    payment_id, _ = await _seed_payment_with_ground_truth(
        migrated_db,
        true_recovery_prob_bps=5000,
        true_failure_type="CUSTOMER_INSUFFICIENT_FUNDS",
        opted_out=True,
    )
    source_event_id = str(uuid.uuid4())
    await process_payment_failure(payment_id, "HDFC", redis_client, source_event_id=source_event_id)
    await process_payment_failure(payment_id, "HDFC", redis_client, source_event_id=source_event_id)

    from sqlalchemy import create_engine

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT count(*) FROM diagnosis_outcomes o "
                "JOIN diagnoses d ON d.diagnosis_id = o.diagnosis_id WHERE d.payment_id = :pid"
            ),
            {"pid": payment_id},
        ).scalar_one()
    assert count == 1
    sync_engine.dispose()
