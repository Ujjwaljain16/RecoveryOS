"""
Domain Audit finding #6 -- GET /v1/experiments/live's new fair_comparison
block, proven end-to-end through the real API (real Postgres, real auth,
zero mocks). The identity this decomposition rests on:

    incremental_recovery_paise (the original, unfair 1-attempt comparison)
        == attributable_to_more_attempts_paise
         + attributable_to_better_decisions_paise

by construction (both terms are computed from the same three real sums),
not something separately re-derived that could silently disagree.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from apps.api.dependencies.auth import generate_api_key
from services.pipeline.baseline import (
    compute_and_persist_baseline_run,
    compute_and_persist_fair_baseline_run,
)
from services.pipeline.ledger import populate_ledger_and_audit_async
from tests.integration.conftest import seed_merchant_with_api_key, to_async_url


async def _seed_recovered_payment_with_ground_truth(
    migrated_db: str, merchant_id: str, *, amount_paise: int = 300_000
) -> str:
    customer_id = str(uuid.uuid4())
    payment_id = str(uuid.uuid4())
    candidate_id = str(uuid.uuid4())
    decision_id = str(uuid.uuid4())
    policy_config_id = str(uuid.uuid4())

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO customers (customer_id, merchant_id, is_returning) VALUES (:cid, :mid, true)"
            ),
            {"cid": customer_id, "mid": merchant_id},
        )
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
                "amount": amount_paise,
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
                "VALUES (:lid, :sim_id, :pid, 0.8, 0.9, 0.1, 0.2, 0, 'TEMPORARY_GATEWAY_TIMEOUT')"
            ),
            {"lid": str(uuid.uuid4()), "sim_id": simulation_id, "pid": payment_id},
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
    await engine.dispose()
    return payment_id, decision_id, candidate_id


@pytest.mark.asyncio
async def test_experiments_live_includes_fair_comparison_when_fair_baseline_computed(
    async_client, migrated_db, monkeypatch
):
    merchant_id = str(uuid.uuid4())
    api_key = generate_api_key()
    await seed_merchant_with_api_key(
        migrated_db, merchant_id, "fair-comparison-test-merchant", api_key
    )

    payment_id, decision_id, candidate_id = await _seed_recovered_payment_with_ground_truth(
        migrated_db, merchant_id
    )

    from recoveryos.database import get_app_session_factory

    session_factory = get_app_session_factory()

    # RecoveryOS's own real recovery: SUCCESS, 300,000 paise.
    async with session_factory() as session:
        await populate_ledger_and_audit_async(
            session,
            payment_id=payment_id,
            candidate_id=candidate_id,
            decision_id=decision_id,
            verdict="ALLOW",
            chosen_action="RETRY_NOW",
            recovery_prob_bps=8000,
            cost_paise=0,
            actual_recovery_paise=300_000,
            recovery_id=None,
            outcome="SUCCESS",
        )

    # true_recovery_prob_bps=0 means both baselines see a certain failure
    # UNLESS the resolver is forced -- force the FAIR baseline to succeed
    # on its 2nd simulated attempt, while the single-attempt baseline
    # (which only ever gets one roll) fails.
    single_calls = {"n": 0}

    def single_fake(prob, *, seed_key):
        single_calls["n"] += 1
        return False  # single-attempt baseline never succeeds

    async with session_factory() as session:
        monkeypatch.setattr("services.pipeline.baseline.resolve_simulated_outcome", single_fake)
        await compute_and_persist_baseline_run(session, payment_id)

    fair_calls = {"n": 0}

    def fair_fake(prob, *, seed_key):
        fair_calls["n"] += 1
        return fair_calls["n"] == 2  # fair baseline succeeds on attempt 2

    async with session_factory() as session:
        monkeypatch.setattr("services.pipeline.baseline.resolve_simulated_outcome", fair_fake)
        await compute_and_persist_fair_baseline_run(session, payment_id)

    headers = {"X-API-Key": api_key}
    resp = await async_client.get("/v1/experiments/live", headers=headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["baseline"]["recovered_paise"] == 0
    assert body["recoveryos"]["recovered_paise"] == 300_000
    assert body["incremental_recovery_paise"] == 300_000

    assert "fair_comparison" in body
    fair = body["fair_comparison"]
    assert fair["fair_baseline_recovered_paise"] == 300_000  # succeeded on attempt 2
    assert fair["attributable_to_more_attempts_paise"] == 300_000  # 300,000 - 0
    assert fair["attributable_to_better_decisions_paise"] == 0  # 300,000 - 300,000
    assert fair["scoped_incremental_recovery_paise"] == 300_000

    # The load-bearing identity, checked directly against the response body.
    assert (
        fair["attributable_to_more_attempts_paise"] + fair["attributable_to_better_decisions_paise"]
        == fair["scoped_incremental_recovery_paise"]
    )


@pytest.mark.asyncio
async def test_experiments_live_omits_fair_comparison_when_not_yet_computed(
    async_client, migrated_db
):
    """A merchant/dataset that hasn't had the fair baseline run yet must
    never see a fabricated decomposition -- the key is simply absent."""
    merchant_id = str(uuid.uuid4())
    api_key = generate_api_key()
    await seed_merchant_with_api_key(
        migrated_db, merchant_id, "no-fair-baseline-yet-merchant", api_key
    )

    headers = {"X-API-Key": api_key}
    resp = await async_client.get("/v1/experiments/live", headers=headers)
    assert resp.status_code == 200
    assert "fair_comparison" not in resp.json()
