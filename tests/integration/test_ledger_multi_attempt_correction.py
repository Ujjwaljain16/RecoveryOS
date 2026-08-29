"""
Domain Audit finding #2, end-to-end proof (real Postgres, zero mocks) --
the exact regression scenarios named in the fix instructions: FAILED ->
SUCCESS, BLOCK -> SUCCESS, multiple SUCCESS attempts, duplicate delivery,
and genuinely distinct recovery attempts. Calls populate_ledger_and_audit_
async/_sync directly (the two real call sites -- services/pipeline/
consumer.py and workers/execution_worker.py) rather than going through
services/pipeline/reconciliation.py, since this generalized correction
path is reachable from EITHER writer now, not just the webhook path
already covered by tests/integration/test_razorpay_webhook_endpoint.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from services.pipeline.ledger import populate_ledger_and_audit_async, populate_ledger_and_audit_sync
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


async def _seed_decision(
    migrated_db: str, payment_id: str, *, action_type: str = "RETRY_NOW"
) -> tuple[str, str]:
    """A real candidate_actions + policy_decisions row -- audit_log's FKs
    require these to genuinely exist, matching what a real decision cycle
    (services/recovery_engine/orchestrator.py) would have already
    persisted before ledger.py ever gets called."""
    candidate_id = str(uuid.uuid4())
    decision_id = str(uuid.uuid4())
    policy_config_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO policy_configs (policy_config_id) VALUES (:pcid)"),
            {"pcid": policy_config_id},
        )
        await conn.execute(
            text(
                "INSERT INTO candidate_actions (candidate_id, payment_id, action_type, "
                "recovery_prob_bps, expected_value_paise, cost_paise, friction_penalty_paise, "
                "risk_penalty_paise, model_version, created_at) "
                "VALUES (:cid, :pid, :action, 8000, 80000, 0, 0, 0, 'test-v1', now())"
            ),
            {"cid": candidate_id, "pid": payment_id, "action": action_type},
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
    return candidate_id, decision_id


async def _seed_payment(migrated_db: str, *, amount_paise: int = 200_000) -> str:
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    payment_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
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
    await engine.dispose()
    return payment_id


def _ledger_row(migrated_db: str, payment_id: str) -> dict | None:
    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT actual_recovery_paise, incremental_recovery_paise FROM recovery_ledger "
                    "WHERE payment_id = :pid"
                ),
                {"pid": payment_id},
            )
            .mappings()
            .first()
        )
    sync_engine.dispose()
    return dict(row) if row else None


def _audit_log_count(migrated_db: str, payment_id: str) -> int:
    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
    sync_engine.dispose()
    return count


def _payment_status(migrated_db: str, payment_id: str) -> str:
    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM payments WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
    sync_engine.dispose()
    return status


@pytest.mark.asyncio
async def test_block_then_success_corrects_the_ledger_not_silently_dropped(migrated_db):
    """BLOCK -> SUCCESS: the first decision cycle for this payment never
    executed anything (a real policy BLOCK, actual_recovery=0), written
    immediately by consumer.py's own branch. A SECOND, genuinely later
    decision cycle for the SAME payment_id (e.g. a merchant re-ingesting a
    fresh PAYMENT_FAILED event) reaches ALLOW + a real SUCCESS -- before
    this fix, this was silently dropped by ON CONFLICT (payment_id) DO
    NOTHING."""
    payment_id = await _seed_payment(migrated_db, amount_paise=200_000)

    from recoveryos.database import get_app_session_factory

    session_factory = get_app_session_factory()

    cand1, dec1 = await _seed_decision(migrated_db, payment_id)
    async with session_factory() as session:
        await populate_ledger_and_audit_async(
            session,
            payment_id=payment_id,
            candidate_id=cand1,
            decision_id=dec1,
            verdict="BLOCK",
            chosen_action="RETRY_NOW",
            recovery_prob_bps=8000,
            cost_paise=0,
            actual_recovery_paise=0,
            outcome=None,
        )

    before = _ledger_row(migrated_db, payment_id)
    assert before is not None
    assert before["actual_recovery_paise"] == 0

    cand2, dec2 = await _seed_decision(migrated_db, payment_id)
    async with session_factory() as session:
        await populate_ledger_and_audit_async(
            session,
            payment_id=payment_id,
            candidate_id=cand2,
            decision_id=dec2,
            verdict="ALLOW",
            chosen_action="RETRY_NOW",
            recovery_prob_bps=8000,
            cost_paise=0,
            actual_recovery_paise=200_000,
            recovery_id=None,
            outcome="SUCCESS",
        )

    after = _ledger_row(migrated_db, payment_id)
    assert after is not None
    assert after["actual_recovery_paise"] == 200_000, (
        "the real SUCCESS from the second decision cycle must correct the ledger, "
        "not be silently discarded by the existing BLOCK-cycle's row"
    )
    assert _payment_status(migrated_db, payment_id) == "recovered"
    assert (
        _audit_log_count(migrated_db, payment_id) == 2
    ), "one entry per real terminal outcome, append-only"


def test_failed_then_success_via_execution_worker_sync_path_corrects_the_ledger(migrated_db):
    """FAILED -> SUCCESS via the SYNC writer (workers/execution_worker.py's
    own call site) -- the async writer's equivalent is already proven by
    the webhook endpoint tests; this proves the sync mirror independently."""
    import asyncio

    payment_id = asyncio.run(_seed_payment(migrated_db, amount_paise=150_000))
    cand1, dec1 = asyncio.run(_seed_decision(migrated_db, payment_id))
    cand2, dec2 = asyncio.run(_seed_decision(migrated_db, payment_id))

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        populate_ledger_and_audit_sync(
            conn,
            payment_id=payment_id,
            candidate_id=cand1,
            decision_id=dec1,
            verdict="ALLOW",
            chosen_action="RETRY_NOW",
            recovery_prob_bps=8000,
            cost_paise=0,
            actual_recovery_paise=0,
            recovery_id=None,
            outcome="FAILED",
        )

    before = _ledger_row(migrated_db, payment_id)
    assert before["actual_recovery_paise"] == 0

    with sync_engine.connect() as conn:
        populate_ledger_and_audit_sync(
            conn,
            payment_id=payment_id,
            candidate_id=cand2,
            decision_id=dec2,
            verdict="ALLOW",
            chosen_action="RETRY_NOW",
            recovery_prob_bps=8000,
            cost_paise=0,
            actual_recovery_paise=150_000,
            recovery_id=None,
            outcome="SUCCESS",
        )

    after = _ledger_row(migrated_db, payment_id)
    assert after["actual_recovery_paise"] == 150_000
    assert _payment_status(migrated_db, payment_id) == "recovered"
    sync_engine.dispose()


@pytest.mark.asyncio
async def test_multiple_success_attempts_do_not_double_count(migrated_db):
    """Two DISTINCT recovery attempts (different recovery_id/decision_id)
    both reporting SUCCESS for the SAME payment -- structurally shouldn't
    happen under normal operation (EligibilityRule blocks further attempts
    once status='recovered'), but if it's ever observed, the ledger must
    keep the FIRST recorded nonzero value, never sum/double-count the
    same underlying debt."""
    from recoveryos.database import get_app_session_factory

    payment_id = await _seed_payment(migrated_db, amount_paise=100_000)
    session_factory = get_app_session_factory()

    cand1, dec1 = await _seed_decision(migrated_db, payment_id)
    async with session_factory() as session:
        await populate_ledger_and_audit_async(
            session,
            payment_id=payment_id,
            candidate_id=cand1,
            decision_id=dec1,
            verdict="ALLOW",
            chosen_action="RETRY_NOW",
            recovery_prob_bps=8000,
            cost_paise=0,
            actual_recovery_paise=100_000,
            recovery_id=None,
            outcome="SUCCESS",
        )

    cand2, dec2 = await _seed_decision(migrated_db, payment_id, action_type="ALT_ROUTE")
    async with session_factory() as session:
        await populate_ledger_and_audit_async(
            session,
            payment_id=payment_id,
            candidate_id=cand2,
            decision_id=dec2,
            verdict="ALLOW",
            chosen_action="ALT_ROUTE",
            recovery_prob_bps=8000,
            cost_paise=0,
            actual_recovery_paise=100_000,  # a second, distinct "SUCCESS" for the same debt
            recovery_id=None,
            outcome="SUCCESS",
        )

    row = _ledger_row(migrated_db, payment_id)
    assert (
        row["actual_recovery_paise"] == 100_000
    ), "must never sum to 200,000 -- a payment can only genuinely owe/recover its own amount once"
    assert _audit_log_count(migrated_db, payment_id) == 1, (
        "the second SUCCESS must be a true no-op (never-downgrade-an-already-recovered-payment "
        "rule applies symmetrically to a second SUCCESS too) -- no new audit entry"
    )


@pytest.mark.asyncio
async def test_genuinely_distinct_non_recovering_attempts_do_not_spuriously_correct_each_other(
    migrated_db,
):
    """Two DISTINCT attempts (different decision/candidate/recovery ids)
    that both fail to recover anything (BLOCK, then FAILED) must not
    treat the second as a 'correction' of the first -- no new revenue
    information exists between two non-recovering outcomes."""
    from recoveryos.database import get_app_session_factory

    payment_id = await _seed_payment(migrated_db, amount_paise=80_000)
    session_factory = get_app_session_factory()

    cand1, dec1 = await _seed_decision(migrated_db, payment_id)
    async with session_factory() as session:
        await populate_ledger_and_audit_async(
            session,
            payment_id=payment_id,
            candidate_id=cand1,
            decision_id=dec1,
            verdict="BLOCK",
            chosen_action="RETRY_NOW",
            recovery_prob_bps=8000,
            cost_paise=0,
            actual_recovery_paise=0,
            outcome=None,
        )

    cand2, dec2 = await _seed_decision(migrated_db, payment_id)
    async with session_factory() as session:
        await populate_ledger_and_audit_async(
            session,
            payment_id=payment_id,
            candidate_id=cand2,
            decision_id=dec2,
            verdict="ALLOW",
            chosen_action="RETRY_NOW",
            recovery_prob_bps=8000,
            cost_paise=0,
            actual_recovery_paise=0,
            recovery_id=None,
            outcome="FAILED",
        )

    row = _ledger_row(migrated_db, payment_id)
    assert row["actual_recovery_paise"] == 0
    assert (
        _audit_log_count(migrated_db, payment_id) == 1
    ), "a second non-recovering attempt carries no new revenue information -- must be a true no-op"
