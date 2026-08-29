"""
Domain Audit finding #6 -- the fair, same-attempt-budget baseline
(services/pipeline/baseline.py:compute_and_persist_fair_baseline_run).
Real Postgres, real simulator_latent_state ground truth, the SAME shared
resolve_simulated_outcome() resolver the rest of the pipeline uses --
only the dice rolls themselves are made deterministic via monkeypatch, so
these tests assert exact attempt counts/outcomes rather than probabilistic
ones.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from services.pipeline.baseline import (
    PIPELINE_BASELINE_FAIR_EXPERIMENT_ID,
    compute_and_persist_baseline_run,
    compute_and_persist_fair_baseline_run,
)
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


async def _seed_payment_with_latent_state(
    migrated_db: str,
    *,
    amount_paise: int = 300_000,
    failure_class: str = "TEMPORARY",
    failure_code: str = "TIMEOUT",
    true_recovery_prob_bps: int = 5000,
    max_retries: int | None = None,
) -> tuple[str, str]:
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    engine = create_async_engine(to_async_url(migrated_db))
    payment_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        if max_retries is not None:
            policy_config_id = str(uuid.uuid4())
            await conn.execute(
                text(
                    "INSERT INTO policy_configs (policy_config_id, max_retries) VALUES (:id, :mr)"
                ),
                {"id": policy_config_id, "mr": max_retries},
            )
            await conn.execute(
                text("UPDATE merchants SET policy_config_id = :pcid WHERE merchant_id = :mid"),
                {"pcid": policy_config_id, "mid": merchant_id},
            )
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, :amount, 'upi', 'HDFC', 'failed', :fcode, :fclass, "
                "true, :ts, :ts)"
            ),
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "amount": amount_paise,
                "fcode": failure_code,
                "fclass": failure_class,
                "ts": datetime.now(UTC) - timedelta(hours=1),
            },
        )
        simulation_id = str(uuid.uuid4())
        await conn.execute(
            text(
                "INSERT INTO simulator_manifests (simulation_id, seed, generator_version, "
                "scenario_config, latent_function_version, total_payments) "
                "VALUES (:sim_id, 1, 'test', '{}'::jsonb, 'test-v1', 1)"
            ),
            {"sim_id": simulation_id},
        )
        await conn.execute(
            text(
                "INSERT INTO simulator_latent_state (latent_id, simulation_id, payment_id, "
                "customer_patience_score, bank_latent_health, latent_network_noise, "
                "latent_customer_propensity, true_recovery_prob_bps, true_failure_type) "
                "VALUES (:lid, :sim_id, :pid, 0.8, 0.9, 0.1, 0.2, :prob, 'TEMPORARY_GATEWAY_TIMEOUT')"
            ),
            {
                "lid": str(uuid.uuid4()),
                "sim_id": simulation_id,
                "pid": payment_id,
                "prob": true_recovery_prob_bps,
            },
        )
    await engine.dispose()
    return payment_id, merchant_id


@pytest.mark.asyncio
async def test_fair_baseline_succeeds_on_a_later_attempt_the_single_attempt_baseline_missed(
    migrated_db, monkeypatch
):
    """The exact scenario finding #6 is about: a naive strategy that only
    gets to try ONCE fails, but the SAME naive strategy given RecoveryOS's
    own attempt budget succeeds on attempt 3 -- proving the old 1-attempt
    baseline understated what a fair naive comparison would recover."""
    payment_id, _ = await _seed_payment_with_latent_state(migrated_db, max_retries=5)

    calls = {"n": 0}

    def fake_resolver(true_recovery_prob_bps: int, *, seed_key: str) -> bool:
        calls["n"] += 1
        return calls["n"] == 3  # fails attempts 1-2, succeeds on attempt 3

    monkeypatch.setattr("services.pipeline.baseline.resolve_simulated_outcome", fake_resolver)

    from recoveryos.database import get_app_session_factory

    session_factory = get_app_session_factory()
    async with session_factory() as session:
        fair_result = await compute_and_persist_fair_baseline_run(session, payment_id)

    assert fair_result["outcome"] == "RECOVERED"
    assert fair_result["attempts_used"] == 3
    assert fair_result["recovered_amount_paise"] == 300_000
    assert (
        calls["n"] == 3
    ), "must stop rolling the dice once it succeeds, not keep going to max_retries"


@pytest.mark.asyncio
async def test_fair_baseline_never_exceeds_the_merchants_own_max_retries(migrated_db, monkeypatch):
    payment_id, _ = await _seed_payment_with_latent_state(migrated_db, max_retries=3)

    monkeypatch.setattr(
        "services.pipeline.baseline.resolve_simulated_outcome", lambda prob, *, seed_key: False
    )  # never succeeds

    from recoveryos.database import get_app_session_factory

    session_factory = get_app_session_factory()
    async with session_factory() as session:
        fair_result = await compute_and_persist_fair_baseline_run(session, payment_id)

    assert fair_result["outcome"] == "NOT_RECOVERED"
    assert fair_result["attempts_used"] == 3, "must consume exactly max_retries attempts, not more"
    assert fair_result["recovered_amount_paise"] == 0


@pytest.mark.asyncio
async def test_fair_baseline_never_attempts_a_known_hopeless_failure(migrated_db, monkeypatch):
    """A PERMANENT failure_class is unretryable per _would_baseline_retry --
    the fair baseline must respect this exactly like the single-attempt
    one, never 'trying harder' against a known-hopeless case."""
    payment_id, _ = await _seed_payment_with_latent_state(
        migrated_db, failure_class="PERMANENT", max_retries=5
    )

    calls = {"n": 0}

    def fake_resolver(true_recovery_prob_bps: int, *, seed_key: str) -> bool:
        calls["n"] += 1
        return True

    monkeypatch.setattr("services.pipeline.baseline.resolve_simulated_outcome", fake_resolver)

    from recoveryos.database import get_app_session_factory

    session_factory = get_app_session_factory()
    async with session_factory() as session:
        fair_result = await compute_and_persist_fair_baseline_run(session, payment_id)

    assert fair_result["outcome"] == "NOT_ATTEMPTED"
    assert fair_result["attempts_used"] == 0
    assert calls["n"] == 0, "must never roll the dice at all for a known-hopeless failure"


@pytest.mark.asyncio
async def test_fair_baseline_recovers_at_least_as_much_as_the_single_attempt_baseline(
    migrated_db, monkeypatch
):
    """The monotonic property that makes the whole comparison meaningful:
    giving the SAME naive strategy MORE chances can never recover LESS
    than giving it one chance, holding the RNG outcomes fixed."""
    payment_id, _ = await _seed_payment_with_latent_state(migrated_db, max_retries=4)

    # Fails attempt 1, succeeds attempt 2 -- the single-attempt baseline
    # only ever sees attempt 1 and must report NOT_RECOVERED; the fair
    # baseline continues and succeeds.
    calls = {"n": 0}

    def fake_resolver(true_recovery_prob_bps: int, *, seed_key: str) -> bool:
        calls["n"] += 1
        return calls["n"] == 2

    monkeypatch.setattr("integrations.razorpay.adapter.resolve_simulated_outcome", fake_resolver)
    monkeypatch.setattr("services.pipeline.baseline.resolve_simulated_outcome", fake_resolver)

    from recoveryos.database import get_app_session_factory

    session_factory = get_app_session_factory()
    async with session_factory() as session:
        single_result = await compute_and_persist_baseline_run(session, payment_id)

    calls["n"] = 0  # reset for the fair run's own independent dice rolls
    async with session_factory() as session:
        fair_result = await compute_and_persist_fair_baseline_run(session, payment_id)

    assert single_result["recovered_amount_paise"] == 0
    assert fair_result["recovered_amount_paise"] == 300_000
    assert fair_result["recovered_amount_paise"] >= single_result["recovered_amount_paise"]


@pytest.mark.asyncio
async def test_fair_and_single_attempt_baselines_are_stored_under_separate_experiment_ids(
    migrated_db, monkeypatch
):
    """Additive, not a silent redefinition -- both rows coexist for the
    same payment_id, under different experiment_id sentinels."""
    payment_id, _ = await _seed_payment_with_latent_state(
        migrated_db, true_recovery_prob_bps=10_000, max_retries=2
    )

    from recoveryos.database import get_app_session_factory

    session_factory = get_app_session_factory()
    async with session_factory() as session:
        await compute_and_persist_baseline_run(session, payment_id)
    async with session_factory() as session:
        await compute_and_persist_fair_baseline_run(session, payment_id)

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT experiment_id, attempts_used FROM baseline_runs WHERE payment_id = :pid"
                ),
                {"pid": payment_id},
            )
        ).fetchall()
    await engine.dispose()

    assert len(rows) == 2
    experiment_ids = {str(r[0]) for r in rows}
    assert PIPELINE_BASELINE_FAIR_EXPERIMENT_ID in experiment_ids
    fair_row = next(r for r in rows if str(r[0]) == PIPELINE_BASELINE_FAIR_EXPERIMENT_ID)
    assert fair_row[1] == 1  # attempts_used, succeeded on attempt 1
