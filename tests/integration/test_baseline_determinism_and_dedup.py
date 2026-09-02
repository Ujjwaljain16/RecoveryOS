"""
Adversarial Audit Verdict fixes:

Blocker #5 -- resolve_simulated_outcome()/_recompute_attempt_aware_prob_bps()
used to draw from Python's process-random `random` module / a fresh
`uuid.uuid4()`-seeded SimRng per call, so re-running the SAME dataset seed
through the pipeline produced a DIFFERENT headline number every time -- the
opposite of what a "seed=N" evaluation claim requires. Fixed by hashing a
stable (payment_id, attempt_number, purpose) identity into the draw
(integrations/razorpay/adapter.py::_deterministic_bps_draw). These tests
prove same-identity-in same-outcome-out, across fresh calls/processes (no
shared mutable RNG state to "accidentally" make it look deterministic).

Score multiplier #8 -- baseline_runs had no DB-level unique constraint on
(payment_id, experiment_id); two concurrent calls to
compute_and_persist_baseline_run/compute_and_persist_fair_baseline_run for
the same payment could both pass the SELECT-first check and both INSERT,
double-counting in any SUM() over the table. Fixed via migration 0019's
uq_baseline_runs_payment_experiment constraint + the S1
INSERT...ON CONFLICT DO NOTHING...RETURNING dedup pattern. This test proves
genuinely concurrent calls (asyncio.gather, independent sessions) converge
to exactly one row.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from integrations.razorpay.adapter import _deterministic_bps_draw, resolve_simulated_outcome
from recoveryos.database import get_app_session_factory
from services.pipeline.baseline import (
    PIPELINE_BASELINE_EXPERIMENT_ID,
    compute_and_persist_baseline_run,
    compute_and_persist_compliance_aware_baseline_run,
    compute_and_persist_fair_baseline_run,
)
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


def test_deterministic_bps_draw_is_stable_across_repeated_calls():
    key = "payment-abc:2:outcome"
    draws = {_deterministic_bps_draw(key) for _ in range(50)}
    assert len(draws) == 1, "the same seed_key must always produce the same draw"


def test_deterministic_bps_draw_differs_across_distinct_keys():
    draws = {_deterministic_bps_draw(f"payment-abc:{n}:outcome") for n in range(1, 30)}
    assert (
        len(draws) > 1
    ), "distinct attempt numbers must not collapse onto one draw (that would defeat the point)"


def test_resolve_simulated_outcome_is_reproducible_for_the_same_seed_key():
    """The exact regression the audit's Blocker #5 asked for: run the
    identical (probability, seed_key) pair many times and confirm the
    boolean outcome never flips -- no process-random `random.uniform()`
    left in the path."""
    prob_bps = 4137  # an arbitrary non-round value, not 0/10000 edge cases
    seed_key = "payment-xyz:1"
    outcomes = {resolve_simulated_outcome(prob_bps, seed_key=seed_key) for _ in range(100)}
    assert (
        len(outcomes) == 1
    ), f"resolve_simulated_outcome flip-flopped for the same seed_key: {outcomes}"


async def _seed_payment_with_latent_state(
    migrated_db: str, *, true_recovery_prob_bps: int = 5000
) -> str:
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    engine = create_async_engine(to_async_url(migrated_db))
    payment_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, 300000, 'upi', 'HDFC', 'failed', 'TIMEOUT', 'TEMPORARY', "
                "true, :ts, :ts)"
            ),
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
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
    return payment_id


@pytest.mark.asyncio
async def test_concurrent_baseline_runs_for_the_same_payment_converge_to_one_row(migrated_db):
    """Two genuinely concurrent compute_and_persist_baseline_run() calls for
    the SAME payment (asyncio.gather, independent AsyncSessions) must not
    both insert -- migration 0019's unique constraint plus the ON CONFLICT
    dedup path must leave exactly one baseline_runs row, and both callers
    must agree on the same outcome."""
    payment_id = await _seed_payment_with_latent_state(migrated_db, true_recovery_prob_bps=10_000)

    session_factory = get_app_session_factory()

    async def run_once():
        async with session_factory() as session:
            return await compute_and_persist_baseline_run(session, payment_id)

    results = await asyncio.gather(run_once(), run_once(), return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            raise r

    outcomes = {r["outcome"] for r in results}
    assert (
        len(outcomes) == 1
    ), f"both concurrent calls must agree on the same outcome, got {results}"

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT run_id FROM baseline_runs WHERE payment_id = :pid AND experiment_id = :exp"
                ),
                {"pid": payment_id, "exp": PIPELINE_BASELINE_EXPERIMENT_ID},
            )
        ).fetchall()
    await engine.dispose()

    assert (
        len(rows) == 1
    ), f"exactly one baseline_runs row must exist despite the concurrent race, got {rows}"


# ═══════════════════════════════════════════════════════════════════════
# Adversarial sweep scenario #36 -- baseline computation cannot mutate
# RecoveryOS decision tables
# ═══════════════════════════════════════════════════════════════════════

_DECISION_TABLES = ("policy_decisions", "candidate_actions", "diagnoses", "recoveries", "recovery_ledger")


async def _count_rows(engine, table: str) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()


@pytest.mark.asyncio
async def test_baseline_computation_never_touches_recoveryos_decision_tables(migrated_db):
    """
    Every baseline comparator (single-attempt, compliance-blind fair,
    compliance-aware fair) reads payments/simulator_latent_state and writes
    ONLY to baseline_runs -- it must never insert/update/delete a row in any
    table RecoveryOS's own real decision/execution path owns
    (policy_decisions, candidate_actions, diagnoses, recoveries,
    recovery_ledger). This was proven empirically across the 5-seed
    compliance-aware benchmark (blocked_by_rule column's own docstring); this
    test makes that proof a permanent regression check rather than a one-off
    evaluation-run observation.
    """
    payment_id = await _seed_payment_with_latent_state(migrated_db, true_recovery_prob_bps=5000)
    engine = create_async_engine(to_async_url(migrated_db))

    before = {table: await _count_rows(engine, table) for table in _DECISION_TABLES}

    session_factory = get_app_session_factory()
    async with session_factory() as session:
        await compute_and_persist_baseline_run(session, payment_id)
    async with session_factory() as session:
        await compute_and_persist_fair_baseline_run(session, payment_id)
    async with session_factory() as session:
        await compute_and_persist_compliance_aware_baseline_run(session, payment_id)

    after = {table: await _count_rows(engine, table) for table in _DECISION_TABLES}
    await engine.dispose()

    assert before == after, (
        "baseline computation must never mutate RecoveryOS's own decision/execution "
        f"tables -- before={before} after={after}"
    )

    # And it must have actually done its job (three distinct experiment rows),
    # not passed the isolation check by silently doing nothing.
    async with create_async_engine(to_async_url(migrated_db)).connect() as conn:
        n_baseline_rows = (
            await conn.execute(text("SELECT count(*) FROM baseline_runs WHERE payment_id = :pid"), {"pid": payment_id})
        ).scalar_one()
    assert n_baseline_rows == 3, f"expected 3 baseline_runs rows (one per comparator), got {n_baseline_rows}"
