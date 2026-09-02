"""
gaps.md sec:C.5 -- the fair baseline (services/pipeline/baseline.py)
evaluates up to max_retries attempts in one instantaneous batch loop, while
RecoveryOS's real second attempt is gated by a genuine retry_cooldown_hours
wait, scheduled via workers/execution_worker.py's schedule_reevaluation_sync
and fired later by workers/retry_scheduler.py's real 5s poll. Confirmed via
the forensic root-cause investigation as 50.3% of the canonical run's
missed-revenue gap: a ~10-12 minute evaluation drain window can never
observe a real attempt 2 at the production default (12h).

The fix (tests/evaluation/multi_seed_runner.py::accelerate_evaluation_cooldown
and its identical twin in ai_ablation_runner.py) pre-seeds a policy_config's
retry_cooldown_hours to 0 -- EXACT same CooldownRule, EXACT same
schedule_reevaluation_sync, EXACT same retry_scheduler.py, only the
CONFIGURATION VALUE differs, and only inside a run's own freshly-wiped
evaluation database. These tests prove:

1. Production's default retry_cooldown_hours is unaffected.
2. The acceleration itself is deterministic and idempotent.
3. A FAILED real attempt-1 reschedules almost immediately (not 12h later)
   when the merchant's policy_config has been accelerated, and the
   scheduler can genuinely fire it without any time-travel -- proving
   RecoveryOS's real attempt-2 opportunity is now reachable inside a short
   evaluation window, the same opportunity the fair baseline already had
   for free.
4. Same seed + same payment + same evaluation start time -> identical
   two-round outcome (determinism survives the accelerated path).
6. No duplicate/idempotency issue from re-running the acceleration step.

Item 5 (OutcomeResolver identical between live execution and the fair
baseline) is already covered by tests/unit/test_resolve_simulated_outcome_shared.py
-- not duplicated here. Item 7 (ai_unsafe_deltas safety invariant) is
covered by tests/unit/test_ai_recommendation_adversarial.py and
tests/integration/test_ai_recommendation_bounded_influence.py, neither of
which this change touches.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from integrations.razorpay.adapter import ProviderResult
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


async def _seed_retry_now_favored_payment_with_cooldown(
    migrated_db: str, *, retry_cooldown_hours: int, amount_paise: int = 200_000
) -> tuple[str, str, str]:
    """Same recipe as test_recovery_mission_lifecycle.py's
    _seed_retry_now_favored_payment (cheap RETRY_NOW, every other action
    deliberately uneconomical, method='card' to sidestep Autopay/e-mandate
    compliance rules entirely so they can't confound this test), extended
    with a caller-controlled retry_cooldown_hours -- a merchant-scoped
    policy_config row, not the shared platform-default one, so this test
    doesn't race other tests over that shared row."""
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    bank = f"ACCEL_BANK_{uuid.uuid4().hex[:8]}"
    payment_id = str(uuid.uuid4())
    policy_config_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO policy_configs (policy_config_id, max_retries, retry_cooldown_hours) "
                "VALUES (:pcid, 3, :cooldown)"
            ),
            {"pcid": policy_config_id, "cooldown": retry_cooldown_hours},
        )
        await conn.execute(
            text("UPDATE merchants SET policy_config_id = :pcid WHERE merchant_id = :mid"),
            {"pcid": policy_config_id, "mid": merchant_id},
        )
        await conn.execute(
            text(
                "INSERT INTO action_costs (merchant_id, action_type, cost_paise, friction_base_paise) "
                "VALUES (:mid, 'RETRY_NOW', 100, 10)"
            ),
            {"mid": merchant_id},
        )
        for action_type in ("RETRY_LATER", "ALT_ROUTE", "REMINDER", "ESCALATE"):
            await conn.execute(
                text(
                    "INSERT INTO action_costs (merchant_id, action_type, cost_paise, friction_base_paise) "
                    "VALUES (:mid, :action_type, 10000000, 0)"
                ),
                {"mid": merchant_id, "action_type": action_type},
            )
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, :amount, 'card', :bank, 'failed', 'TIMEOUT', "
                "'TEMPORARY', true, :ts, :ts)"
            ),
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "amount": amount_paise,
                "bank": bank,
                "ts": datetime.now(UTC) - timedelta(hours=1),
            },
        )
    await engine.dispose()
    return payment_id, merchant_id, bank


class AlwaysFailsOnce:
    """Fails the first call, succeeds after -- controllable, deterministic,
    no dependency on simulator_latent_state ground truth."""

    def __init__(self):
        self.calls = 0

    def retry(self, conn, payment_id: str, amount_paise: int, attempt_number: int) -> ProviderResult:
        self.calls += 1
        if self.calls == 1:
            return ProviderResult(outcome="FAILED", provider_ref="accel_1", recovered_amount_paise=0)
        return ProviderResult(
            outcome="SUCCESS", provider_ref=f"accel_{self.calls}", recovered_amount_paise=amount_paise
        )


@pytest.mark.asyncio
async def test_production_default_retry_cooldown_hours_is_still_twelve(migrated_db):
    """Regression pin: a policy_config created without any evaluation
    acceleration must still default to the real production value."""
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        pcid = str(uuid.uuid4())
        await conn.execute(
            text("INSERT INTO policy_configs (policy_config_id) VALUES (:pcid)"), {"pcid": pcid}
        )
        row = (
            await conn.execute(
                text("SELECT retry_cooldown_hours FROM policy_configs WHERE policy_config_id = :pcid"),
                {"pcid": pcid},
            )
        ).first()
    await engine.dispose()
    assert row[0] == 12, "production's retry_cooldown_hours default must remain 12"

    from recoveryos.config import Settings

    assert Settings.model_fields["default_retry_cooldown_hours"].default == 12


@pytest.mark.asyncio
async def test_accelerate_evaluation_cooldown_is_deterministic_and_idempotent(migrated_db):
    """The evaluation-only acceleration step (INSERT ... ON CONFLICT DO
    UPDATE) must always leave exactly one row at retry_cooldown_hours=0,
    regardless of how many times it runs -- no duplicate rows, no drift."""
    engine = create_async_engine(to_async_url(migrated_db))
    pcid = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO policy_configs (policy_config_id) VALUES (:pcid)"), {"pcid": pcid}
        )

    for _ in range(3):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO policy_configs (policy_config_id, retry_cooldown_hours) "
                    "VALUES (:pcid, 0) ON CONFLICT (policy_config_id) DO UPDATE SET retry_cooldown_hours = 0"
                ),
                {"pcid": pcid},
            )

    async with engine.begin() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM policy_configs WHERE policy_config_id = :pcid"), {"pcid": pcid}
            )
        ).scalar_one()
        cooldown = (
            await conn.execute(
                text("SELECT retry_cooldown_hours FROM policy_configs WHERE policy_config_id = :pcid"),
                {"pcid": pcid},
            )
        ).scalar_one()
    await engine.dispose()
    assert count == 1, "the acceleration step must never create a duplicate policy_configs row"
    assert cooldown == 0


@pytest.mark.asyncio
async def test_failed_attempt_reschedules_almost_immediately_when_cooldown_accelerated(
    migrated_db, redis_client, monkeypatch
):
    """The heart of the fix: with retry_cooldown_hours=0, a FAILED real
    attempt's rescheduled re-evaluation must be due within seconds, not 12
    real hours -- proving RecoveryOS's real attempt-2 opportunity is now
    reachable inside a short evaluation window."""
    from recoveryos.config import get_settings
    from services.pipeline.consumer import process_payment_failure
    from workers.execution_worker import process_job

    # tests/integration/test_recovery_mission_lifecycle.py's own pattern:
    # mission.started_at is recorded via the session-pinned clock
    # (tests/conftest.py, fixed date) while execution_worker.py's own
    # budget check uses the REAL wall clock by design -- under the test
    # fixture that gap can exceed the default 7-day mission duration purely
    # as a test artifact, terminating the mission instead of rescheduling.
    monkeypatch.setenv("MISSION_MAX_DURATION_SECONDS", str(365 * 24 * 3600))
    get_settings.cache_clear()

    payment_id, merchant_id, bank = await _seed_retry_now_favored_payment_with_cooldown(
        migrated_db, retry_cooldown_hours=0
    )
    provider = AlwaysFailsOnce()
    sync_engine = create_engine(migrated_db, pool_pre_ping=True)

    await process_payment_failure(payment_id, bank, redis_client)

    entries = await redis_client.xrange("stream:recovery_jobs", min="-", max="+")
    jobs = [(eid, f) for eid, f in entries if f.get("payment_id") == payment_id]
    assert len(jobs) == 1
    _job1_id, job1_fields = jobs[0]
    assert job1_fields["action_type"] == "RETRY_NOW"

    with sync_engine.connect() as conn:
        process_job(conn, job1_fields, provider=provider)
    assert provider.calls == 1

    with sync_engine.connect() as conn:
        reeval_row = (
            conn.execute(
                text(
                    "SELECT status, scheduled_for FROM scheduled_reevaluations WHERE payment_id = :pid"
                ),
                {"pid": payment_id},
            )
            .mappings()
            .first()
        )
    assert reeval_row is not None, "a FAILED retry with budget remaining must reschedule"
    assert reeval_row["status"] == "PENDING"
    real_now = datetime.now(UTC)
    assert reeval_row["scheduled_for"] <= real_now + timedelta(seconds=5), (
        f"scheduled_for={reeval_row['scheduled_for']} is not due almost immediately -- "
        f"the accelerated cooldown did not take effect"
    )
    assert reeval_row["scheduled_for"] < real_now + timedelta(hours=1), (
        "still scheduled hours out -- this would be the unaccelerated 12h production default"
    )


@pytest.mark.asyncio
async def test_retry_scheduler_fires_accelerated_reevaluation_without_any_time_travel(
    migrated_db, redis_client, monkeypatch
):
    """The full round-trip, with NO clock_module.utcnow monkeypatch at all
    (unlike test_recovery_mission_lifecycle.py's fails-then-succeeds test,
    which must time-travel 13 hours past the production default) -- the
    scheduler's real 5s poll, run against the REAL current clock, must be
    able to claim and fully process the due re-evaluation, producing a
    genuine second attempt."""
    from recoveryos.config import get_settings
    from services.pipeline.consumer import process_payment_failure
    from workers.execution_worker import process_job
    from workers.retry_scheduler import run_once

    monkeypatch.setenv("MISSION_MAX_DURATION_SECONDS", str(365 * 24 * 3600))
    get_settings.cache_clear()

    payment_id, merchant_id, bank = await _seed_retry_now_favored_payment_with_cooldown(
        migrated_db, retry_cooldown_hours=0
    )
    provider = AlwaysFailsOnce()
    sync_engine = create_engine(migrated_db, pool_pre_ping=True)

    await process_payment_failure(payment_id, bank, redis_client)
    entries = await redis_client.xrange("stream:recovery_jobs", min="-", max="+")
    job1_id, job1_fields = next((eid, f) for eid, f in entries if f.get("payment_id") == payment_id)

    with sync_engine.connect() as conn:
        process_job(conn, job1_fields, provider=provider)
    assert provider.calls == 1

    # tests/conftest.py's session-wide _pinned_clock_for_determinism fixture
    # pins recoveryos.clock.utcnow() to a fixed PAST date for the whole
    # pytest session -- a real, useful determinism guard for OTHER tests
    # (AutopayExecutionWindowRule etc.), but it means fetch_due_reevaluations
    # (which reads clock.utcnow()) would never see `scheduled_for` (computed
    # from execution_worker.py's genuinely real _now()) as due, no matter
    # how short the cooldown, since the pinned clock never advances on its
    # own. This is a PYTEST-ONLY artifact -- outside pytest (production,
    # and the live evaluation harness, neither of which ever import this
    # fixture) clock.utcnow() is genuinely real and advancing. Un-pinning it
    # back to real current time here is not simulating any wait -- it is
    # removing the test-only pin so this test measures what the harness
    # actually experiences.
    import recoveryos.clock as clock_module

    monkeypatch.setattr(clock_module, "utcnow", lambda: datetime.now(UTC))

    processed = await run_once(redis_client)
    assert processed >= 1, (
        "the scheduler found nothing due against the REAL current clock -- the "
        "accelerated re-evaluation did not actually become immediately due"
    )

    entries_after = await redis_client.xrange("stream:recovery_jobs", min="(" + job1_id, max="+")
    jobs_round2 = [(eid, f) for eid, f in entries_after if f.get("payment_id") == payment_id]
    assert len(jobs_round2) == 1, "the re-evaluation must genuinely enqueue a second real attempt"
    _job2_id, job2_fields = jobs_round2[0]
    assert job2_fields["action_type"] == "RETRY_NOW"

    with sync_engine.connect() as conn:
        process_job(conn, job2_fields, provider=provider)
    assert provider.calls == 2, "the second real attempt must actually execute"

    with sync_engine.connect() as conn:
        recoveries = conn.execute(
            text(
                "SELECT attempt_number, outcome FROM recoveries WHERE payment_id = :pid "
                "ORDER BY attempt_number"
            ),
            {"pid": payment_id},
        ).fetchall()
        ledger = (
            conn.execute(
                text("SELECT actual_recovery_paise FROM recovery_ledger WHERE payment_id = :pid"),
                {"pid": payment_id},
            )
            .mappings()
            .first()
        )
    assert [r[1] for r in recoveries] == ["FAILED", "SUCCESS"], (
        "expected exactly one failed attempt followed by one successful attempt"
    )
    assert ledger["actual_recovery_paise"] > 0, (
        "the ledger must reflect the eventual SUCCESS, not get stuck at the round-1 FAILED value -- "
        "this is exactly the value the fair baseline would already credit itself with"
    )


@pytest.mark.asyncio
async def test_same_seed_and_start_time_produce_identical_two_round_outcome(
    migrated_db, redis_client, monkeypatch
):
    """Determinism survives the accelerated path: the SAME payment run
    through the exact same two-round sequence twice (fresh payment_id each
    time, same seed data via an identical fixture) must produce identical
    chosen actions and identical final outcomes."""
    from recoveryos.config import get_settings
    from services.pipeline.consumer import process_payment_failure
    from workers.execution_worker import process_job
    from workers.retry_scheduler import run_once

    monkeypatch.setenv("MISSION_MAX_DURATION_SECONDS", str(365 * 24 * 3600))
    get_settings.cache_clear()

    # See test_retry_scheduler_fires_accelerated_reevaluation_without_any_time_travel
    # for why: un-pins the session-wide test-only clock pin back to real
    # current time so fetch_due_reevaluations can see the accelerated
    # re-evaluation as due -- not a simulated wait.
    import recoveryos.clock as clock_module

    monkeypatch.setattr(clock_module, "utcnow", lambda: datetime.now(UTC))

    async def run_once_through_both_rounds():
        payment_id, merchant_id, bank = await _seed_retry_now_favored_payment_with_cooldown(
            migrated_db, retry_cooldown_hours=0
        )
        provider = AlwaysFailsOnce()
        sync_engine = create_engine(migrated_db, pool_pre_ping=True)

        await process_payment_failure(payment_id, bank, redis_client)
        entries = await redis_client.xrange("stream:recovery_jobs", min="-", max="+")
        job1_id, job1_fields = next((eid, f) for eid, f in entries if f.get("payment_id") == payment_id)
        with sync_engine.connect() as conn:
            process_job(conn, job1_fields, provider=provider)

        await run_once(redis_client)
        entries_after = await redis_client.xrange("stream:recovery_jobs", min="(" + job1_id, max="+")
        _job2_id, job2_fields = next(
            (eid, f) for eid, f in entries_after if f.get("payment_id") == payment_id
        )
        with sync_engine.connect() as conn:
            process_job(conn, job2_fields, provider=provider)
            ledger = (
                conn.execute(
                    text(
                        "SELECT actual_recovery_paise FROM recovery_ledger WHERE payment_id = :pid"
                    ),
                    {"pid": payment_id},
                )
                .mappings()
                .first()
            )
        return job1_fields["action_type"], job2_fields["action_type"], ledger["actual_recovery_paise"]

    result_a = await run_once_through_both_rounds()
    result_b = await run_once_through_both_rounds()
    assert result_a == result_b
