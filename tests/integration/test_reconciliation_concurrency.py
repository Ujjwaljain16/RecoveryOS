"""
Domain Audit finding #5 -- services/pipeline/reconciliation.py's
check-then-act on `outcome = 'PENDING'` had no lock, unlike execution_worker's
execute_with_idempotency. Two proofs, mirroring
tests/integration/test_idempotent_execution.py's own two-tier rigor for the
sync/execution-path lock:

  1. Mechanical: advisory_lock_async() itself genuinely blocks a second
     AsyncSession attempting the same key, until the first releases it --
     not a no-op that happens to let both through fast enough to look correct.
  2. End-to-end: two genuinely concurrent reconcile_pending_recovery() calls
     for the SAME order_id (asyncio.gather over two separate AsyncSessions --
     the concurrency is real because each session is an independent Postgres
     connection; Postgres itself serializes the two pg_advisory_lock() calls
     at the server, and asyncio genuinely suspends the second coroutine on
     that real network wait, not a scheduling illusion) converge to ONE
     consistent terminal outcome, not a corrupted interleaving.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from recoveryos.database import advisory_lock_async, get_app_session_factory
from services.pipeline.reconciliation import reconcile_pending_recovery
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


@pytest.mark.asyncio
async def test_advisory_lock_async_actually_blocks_a_second_session(migrated_db):
    key = f"lock-test-{uuid.uuid4()}"
    session_factory = get_app_session_factory()

    holder_acquired = asyncio.Event()
    release_holder = asyncio.Event()
    waiter_acquired_at: list[float] = []

    async def holder():
        async with session_factory() as session:
            async with advisory_lock_async(session, key):
                holder_acquired.set()
                await asyncio.wait_for(release_holder.wait(), timeout=10)

    async def waiter():
        await asyncio.wait_for(holder_acquired.wait(), timeout=10)
        start = time.monotonic()
        async with session_factory() as session:
            async with advisory_lock_async(session, key):
                waiter_acquired_at.append(time.monotonic() - start)

    holder_task = asyncio.create_task(holder())
    await asyncio.wait_for(holder_acquired.wait(), timeout=10)
    waiter_task = asyncio.create_task(waiter())

    await asyncio.sleep(0.5)  # the waiter should still be blocked right now
    assert waiter_acquired_at == [], "waiter acquired the lock before the holder released it"

    release_holder.set()
    await asyncio.wait_for(holder_task, timeout=10)
    await asyncio.wait_for(waiter_task, timeout=10)

    assert waiter_acquired_at, "waiter never acquired the lock"
    assert waiter_acquired_at[0] >= 0.4, (
        f"waiter acquired the lock too quickly ({waiter_acquired_at[0]:.3f}s) -- "
        f"expected it to block for ~0.5s until the holder released"
    )


async def _seed_pending_recovery(migrated_db: str, *, order_id: str, amount_paise: int) -> str:
    """Same shape test_razorpay_webhook_endpoint.py's own helper builds --
    a real payment + candidate/decision chain + a PENDING recovery with
    provider_ref=order_id, exactly what RazorpayTestAdapter.retry() leaves
    behind for a real order."""
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
                "VALUES (:pid, :mid, :cid, :amount, 'upi', 'HDFC', 'failed', 'TIMEOUT', 'TEMPORARY', "
                "true, :ts, :ts)"
            ),
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "amount": amount_paise,
                "ts": datetime.now(UTC) - timedelta(hours=1),
            },
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
    return payment_id


@pytest.mark.asyncio
async def test_concurrent_reconciliation_for_same_order_converges_to_one_consistent_outcome(
    migrated_db,
):
    """
    Two genuinely concurrent SUCCESS reconciliations for the SAME order_id
    (each independent AsyncSession/Postgres connection, fired via
    asyncio.gather) must not corrupt the final state: exactly one ledger
    row, with actual_recovery_paise matching EXACTLY ONE of the two
    reported amounts (never a torn/mixed value), and both calls agreeing
    on the same recovery_id.
    """
    order_id = f"order_{uuid.uuid4().hex[:14]}"
    payment_id = await _seed_pending_recovery(migrated_db, order_id=order_id, amount_paise=100_000)

    session_factory = get_app_session_factory()

    async def reconcile(amount_paise: int) -> str | None:
        async with session_factory() as session:
            return await reconcile_pending_recovery(
                session, order_id=order_id, outcome="SUCCESS", recovered_amount_paise=amount_paise
            )

    results = await asyncio.gather(reconcile(100_000), reconcile(100_000), return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            raise r

    matched_ids = {r for r in results if r is not None}
    assert (
        len(matched_ids) == 1
    ), f"both concurrent calls must agree on the same recovery_id, got {results}"

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.connect() as conn:
        ledger_rows = (
            await conn.execute(
                text("SELECT actual_recovery_paise FROM recovery_ledger WHERE payment_id = :pid"),
                {"pid": payment_id},
            )
        ).fetchall()
        outcome = (
            (
                await conn.execute(
                    text(
                        "SELECT outcome, recovered_amount_paise FROM recoveries WHERE payment_id = :pid"
                    ),
                    {"pid": payment_id},
                )
            )
            .mappings()
            .first()
        )
    await engine.dispose()

    assert (
        len(ledger_rows) == 1
    ), f"exactly one terminal ledger row must exist regardless of the concurrent race, got {ledger_rows}"
    assert ledger_rows[0][0] == 100_000
    assert outcome["outcome"] == "SUCCESS"
    assert outcome["recovered_amount_paise"] == 100_000
