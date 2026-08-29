"""
Task E1 (Phase 8, PRD §37 Scenario 4 fix) — a duplicate PAYMENT_FAILED
event for a payment RecoveryOS already successfully recovered must be
BLOCKed unconditionally, not just delayed by CooldownRule.

Phase 8's live evaluation proved this was open: publishing a genuinely new
event (fresh event_id/idempotency_key) for an already-`SUCCESS`-recovered
payment produced a second diagnosis/policy_decision row, and the BLOCK that
happened only happened to occur because CooldownRule's 12h window hadn't
elapsed yet -- rule_trace showed "elapsed=2:50:15 < cooldown=12:00:00", not
anything that knew the payment was already recovered. Past 12h, nothing
would have stopped a second real execution attempt.

The fix: services/pipeline/ledger.py sets payments.status='recovered' on a
real SUCCESS outcome, and EligibilityRule (already first in RULES, ordered
before CooldownRule) now explicitly blocks on that status. These two tests
reproduce the exact original repro (past the cooldown window) and the
"immediately after, still within cooldown" case, asserting the rule_trace
names EligibilityRule specifically -- not a coincidental CooldownRule catch.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from services.pipeline.consumer import process_payment_failure
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


async def _seed_payment_guaranteed_to_recover(migrated_db: str) -> tuple[str, str, str]:
    """
    Same shape as test_pipeline_e2e.py's fixture, but true_recovery_prob_bps
    is pinned at 10000 (100%) so the execution worker's dice roll
    deterministically lands on SUCCESS -- this test needs a REAL 'recovered'
    payment, not a coin flip.
    """
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    engine = create_async_engine(to_async_url(migrated_db))
    payment_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE customers SET is_returning = true WHERE customer_id = :cid"),
            {"cid": customer_id},
        )
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
                "VALUES (:lid, :sim_id, :pid, 0.9, 0.9, 0.0, 0.3, 10000, 'TEMPORARY_GATEWAY_TIMEOUT')"
            ),
            {"lid": str(uuid.uuid4()), "sim_id": simulation_id, "pid": payment_id},
        )
    await engine.dispose()
    return payment_id, merchant_id, customer_id


async def _drive_payment_to_real_success(migrated_db: str, redis_client, payment_id: str) -> None:
    """Run the real pipeline + execution worker until this payment has a
    genuine recoveries.outcome='SUCCESS' row and recovery_ledger exists."""
    await process_payment_failure(payment_id, "HDFC", redis_client)

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        pending_job = conn.execute(
            text("SELECT count(*) FROM recoveries WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()

    if pending_job == 0:
        import redis as sync_redis

        from recoveryos.config import get_settings
        from workers.execution_worker import run_worker

        settings = get_settings()
        sync_client = sync_redis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
        run_worker(sync_client, max_iterations=1)
        sync_client.close()

    with sync_engine.connect() as conn:
        outcome = conn.execute(
            text("SELECT outcome FROM recoveries WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one_or_none()
        status = conn.execute(
            text("SELECT status FROM payments WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
    sync_engine.dispose()

    assert outcome == "SUCCESS", (
        f"test setup requires a real SUCCESS outcome (true_recovery_prob_bps=10000 should "
        f"guarantee this) -- got {outcome!r}"
    )
    assert status == "recovered", (
        f"payments.status must be 'recovered' after a real SUCCESS outcome -- got {status!r} "
        f"(this is the E1 fix itself; if this fails, ledger.py's status UPDATE isn't firing)"
    )


@pytest.mark.asyncio
async def test_already_recovered_payment_blocked_even_immediately_after_recovery(
    migrated_db, redis_client
):
    """
    The case that matters most: block must hold even WITHIN the cooldown
    window, proving it's EligibilityRule doing the work, not an accidental
    overlap with CooldownRule (which would also fail here, but must not be
    the one that fires first).
    """
    payment_id, merchant_id, customer_id = await _seed_payment_guaranteed_to_recover(migrated_db)
    await _drive_payment_to_real_success(migrated_db, redis_client, payment_id)

    # Immediately (well within any cooldown window), a genuinely new event
    # arrives for the same payment -- fresh source_event_id, real ingest path.
    new_source_event_id = str(uuid.uuid4())
    await process_payment_failure(
        payment_id, "HDFC", redis_client, source_event_id=new_source_event_id
    )

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        decision = (
            conn.execute(
                text(
                    "SELECT verdict, rule_trace FROM policy_decisions "
                    "WHERE payment_id = :pid AND source_event_id = :sid"
                ),
                {"pid": payment_id, "sid": new_source_event_id},
            )
            .mappings()
            .first()
        )
    sync_engine.dispose()

    assert decision is not None, "the new event should have produced its own policy_decisions row"
    assert (
        decision["verdict"] == "BLOCK"
    ), f"expected BLOCK for an already-recovered payment, got {decision['verdict']!r}"
    assert len(decision["rule_trace"]) == 1, (
        f"trace should stop at the FIRST rule (EligibilityRule) -- got {len(decision['rule_trace'])} "
        f"entries: {decision['rule_trace']}"
    )
    first = decision["rule_trace"][0]
    assert first["rule"] == "EligibilityRule", (
        f"expected EligibilityRule to be the blocking rule, got {first['rule']!r} -- "
        f"if this is CooldownRule, the fix is relying on cooldown overlap by accident"
    )
    assert "already successfully recovered" in first["reason"]


@pytest.mark.asyncio
async def test_scenario4_repro_duplicate_event_past_cooldown_window_now_blocks_correctly(
    migrated_db, redis_client
):
    """
    Exact reproduction of Phase 8's original live finding: a duplicate event
    arrives AFTER CooldownRule's 12h window has elapsed. Before this fix,
    this sailed through to a second ALLOW/execution. Backdates the real
    recovery's executed_at by 13 hours (same technique as the live proof)
    instead of waiting or mocking wall-clock time.
    """
    payment_id, merchant_id, customer_id = await _seed_payment_guaranteed_to_recover(migrated_db)
    await _drive_payment_to_real_success(migrated_db, redis_client, payment_id)

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        conn.execute(
            text(
                "UPDATE recoveries SET executed_at = :ts, scheduled_for = :ts "
                "WHERE payment_id = :pid"
            ),
            {"ts": datetime.now(UTC) - timedelta(hours=13), "pid": payment_id},
        )
        conn.commit()
    sync_engine.dispose()

    new_source_event_id = str(uuid.uuid4())
    await process_payment_failure(
        payment_id, "HDFC", redis_client, source_event_id=new_source_event_id
    )

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        decision = (
            conn.execute(
                text(
                    "SELECT verdict, rule_trace FROM policy_decisions "
                    "WHERE payment_id = :pid AND source_event_id = :sid"
                ),
                {"pid": payment_id, "sid": new_source_event_id},
            )
            .mappings()
            .first()
        )
        recovery_count = conn.execute(
            text("SELECT count(*) FROM recoveries WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
    sync_engine.dispose()

    assert decision is not None
    assert decision["verdict"] == "BLOCK", (
        f"past the cooldown window, this used to sail through -- expected BLOCK, "
        f"got {decision['verdict']!r}"
    )
    trace = decision["rule_trace"]
    assert trace[0]["rule"] == "EligibilityRule" and trace[0]["passed"] is False, (
        f"must be blocked by EligibilityRule specifically (not CooldownRule, which would "
        f"now PASS since 13h > 12h cooldown) -- got trace: {trace}"
    )
    assert "already successfully recovered" in trace[0]["reason"]
    assert (
        recovery_count == 1
    ), "no second recovery attempt should ever have been enqueued/executed for this payment"
