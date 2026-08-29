"""
Production Architecture Domain Audit, Finding #2 -- recovery_attempts_total/
recovery_success_total used to increment BEFORE the DB commit that backs
the `recoveries` row they claim to count. If the process crashed (or any
exception propagated) between that old increment point and the commit,
Redis would redeliver the message, action_fn would run again from
scratch, and the counter would be permanently over-counted even though
`recoveries` itself stayed correctly deduplicated via its
UNIQUE(idempotency_key) constraint.

Fixed by moving both increments to AFTER the commit that actually backs
_upsert_recovery's write. This test proves the invariant directly: a
provider failure/crash BEFORE that commit must leave the counter
untouched, and a subsequent real redelivery (the same job, same
idempotency_key) must increment it by exactly 1, not 2.
"""

from __future__ import annotations

import uuid

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import create_engine, text

from tests.integration.test_execution_worker import _seed_decision_fk_chain, _seed_failed_payment
from workers.execution_worker import process_job


class CrashingProvider:
    """Simulates a crash (or any unhandled exception) partway through the
    provider call -- BEFORE the commit that backs the recoveries row and
    the metric increments that now depend on it."""

    def retry(self, conn, payment_id, amount_paise, attempt_number):
        raise RuntimeError("simulated crash mid-execution, before the terminal commit")


class NormalProvider:
    def retry(self, conn, payment_id, amount_paise, attempt_number):
        from integrations.razorpay.adapter import ProviderResult

        return ProviderResult(
            outcome="SUCCESS",
            provider_ref=f"order_{uuid.uuid4().hex[:8]}",
            recovered_amount_paise=amount_paise,
        )


def _make_job(
    payment_id: str, decision_id: str, amount_paise: int = 100_000, attempt_number: int = 1
) -> dict:
    return {
        "payment_id": payment_id,
        "idempotency_key": f"recovery:{payment_id}:RETRY_NOW:{attempt_number}",
        "action_type": "RETRY_NOW",
        "attempt_number": attempt_number,
        "decision_id": decision_id,
        "amount_paise": amount_paise,
    }


@pytest.mark.asyncio
async def test_crash_before_commit_then_real_redelivery_increments_exactly_once(migrated_db):
    payment_id = await _seed_failed_payment(migrated_db)
    decision_id = await _seed_decision_fk_chain(migrated_db, payment_id, 100_000)
    job = _make_job(payment_id, decision_id)

    engine = create_engine(migrated_db, pool_pre_ping=True)

    before = (
        REGISTRY.get_sample_value("recovery_attempts_total", {"action_type": "RETRY_NOW"}) or 0.0
    )

    # First delivery: the provider "crashes" before the terminal commit.
    # process_job must raise (nothing committed), and the counter must be
    # completely untouched -- this is the exact scenario the old code got
    # wrong (it had already incremented by this point).
    with engine.connect() as conn, pytest.raises(RuntimeError):
        process_job(conn, job, provider=CrashingProvider())

    after_crash = REGISTRY.get_sample_value("recovery_attempts_total", {"action_type": "RETRY_NOW"})
    assert (
        after_crash == before
    ), "a crash before the terminal commit must never increment the counter"

    with engine.connect() as conn:
        recoveries_count = conn.execute(
            text("SELECT count(*) FROM recoveries WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
    assert recoveries_count == 0, "no recoveries row should exist after the crashed attempt"

    # Real redelivery: Redis (or the reclaim loop) hands the SAME job back
    # for a genuine retry. This must succeed, write exactly one recoveries
    # row, and increment the counter by exactly 1 -- not 2, and not 0.
    with engine.connect() as conn:
        process_job(conn, job, provider=NormalProvider())

    after_redelivery = REGISTRY.get_sample_value(
        "recovery_attempts_total", {"action_type": "RETRY_NOW"}
    )
    assert after_redelivery == before + 1.0, (
        f"exactly one real committed attempt must increment the counter by exactly 1 -- "
        f"before={before}, after_crash={after_crash}, after_redelivery={after_redelivery}"
    )

    with engine.connect() as conn:
        recoveries_count = conn.execute(
            text("SELECT count(*) FROM recoveries WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
    assert recoveries_count == 1, "exactly one recoveries row, matching the counter"
    engine.dispose()


@pytest.mark.asyncio
async def test_success_counter_also_survives_a_crash_before_commit(migrated_db):
    payment_id = await _seed_failed_payment(migrated_db)
    decision_id = await _seed_decision_fk_chain(migrated_db, payment_id, 100_000)
    job = _make_job(payment_id, decision_id)

    engine = create_engine(migrated_db, pool_pre_ping=True)

    before = (
        REGISTRY.get_sample_value("recovery_success_total", {"action_type": "RETRY_NOW"}) or 0.0
    )

    with engine.connect() as conn, pytest.raises(RuntimeError):
        process_job(conn, job, provider=CrashingProvider())

    assert (
        REGISTRY.get_sample_value("recovery_success_total", {"action_type": "RETRY_NOW"}) == before
    )

    with engine.connect() as conn:
        process_job(conn, job, provider=NormalProvider())

    after = REGISTRY.get_sample_value("recovery_success_total", {"action_type": "RETRY_NOW"})
    assert after == before + 1.0
    engine.dispose()


@pytest.mark.asyncio
async def test_a_genuine_redelivery_of_an_already_committed_job_never_double_counts(migrated_db):
    """The OTHER redelivery case: the first attempt fully succeeds and
    commits, but the process crashes/hangs before XACK'ing the Redis
    message (or the reclaim loop just redelivers it again anyway). This
    must be a true no-op on the second call -- get_existing() finds the
    already-committed row and action_fn (where the increments now live)
    never runs a second time."""
    payment_id = await _seed_failed_payment(migrated_db)
    decision_id = await _seed_decision_fk_chain(migrated_db, payment_id, 100_000)
    job = _make_job(payment_id, decision_id)

    engine = create_engine(migrated_db, pool_pre_ping=True)
    before = (
        REGISTRY.get_sample_value("recovery_attempts_total", {"action_type": "RETRY_NOW"}) or 0.0
    )

    with engine.connect() as conn:
        process_job(conn, job, provider=NormalProvider())
    with engine.connect() as conn:
        process_job(conn, job, provider=NormalProvider())  # genuine redelivery of the SAME job

    after = REGISTRY.get_sample_value("recovery_attempts_total", {"action_type": "RETRY_NOW"})
    assert after == before + 1.0, "a redelivery of an already-committed job must never re-increment"
    engine.dispose()
