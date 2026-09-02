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
    # migration 0025 -- a successfully processed claim now reaches the
    # terminal COMPLETED status (adversarial sweep finding #50's fix),
    # not FIRED forever; FIRED is now specifically "claimed, still in
    # flight or crashed," never the resting state of a healthy row.
    assert fired_row["status"] == "COMPLETED"
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


# ═══════════════════════════════════════════════════════════════════════
# Adversarial sweep scenario #50 -- orphaned scheduled reevaluations
#
# migration 0025 / services/recovery_engine/scheduling.py's lease mechanism:
# a claimed (FIRED) row is no longer a permanent orphan on crash. These
# tests replace the earlier "proves the orphan is permanent" regression
# (that behavior no longer exists) with proof of the fixed guarantee: a
# reclaimed row is either safely reprocessed to completion (mission still
# waiting) or safely cancelled without reprocessing (mission already moved
# on by some other path) -- never left stuck, and never double-processed.
# ═══════════════════════════════════════════════════════════════════════


async def _create_mission_in_observing_outcome_and_link(
    migrated_db: str, *, payment_id: str, reevaluation_id: str, amount_paise: int = 500_000
) -> str:
    """
    tests/integration/test_retry_scheduler.py's existing fixture calls
    decide_and_persist() directly (deliberately isolating the scheduling
    mechanism from mission tracking, which is services/pipeline/consumer.py's
    responsibility, not decide_and_persist's) -- no mission exists in that
    flow at all. The new lease/reclaim tests need a REAL mission genuinely
    sitting in OBSERVING_OUTCOME (the state the reclaim safety check reads),
    so this helper creates one directly and walks it through the full
    code-owned transition chain (services/recovery_engine/mission.py's
    ALLOWED_TRANSITIONS), then links the already-created scheduled_reevaluations
    row to it -- the same mission_id linkage services/recovery_engine/orchestrator.py's
    real RETRY_LATER path establishes at creation time.
    """
    from recoveryos.database import get_app_session_factory
    from services.recovery_engine.mission import get_or_create_mission_async, transition_mission_async

    now = datetime.now(UTC)
    async with get_app_session_factory()() as session:
        mission, _created = await get_or_create_mission_async(
            session,
            payment_id=payment_id,
            amount_paise=amount_paise,
            now=now,
            max_investigation_rounds=10,
            max_attempts=10,
            max_mission_duration_seconds=365 * 24 * 3600,
        )
        mission_id = mission["mission_id"]
        for to_state, event_type in (
            ("INVESTIGATING", "test_setup"),
            ("PLANNING", "test_setup"),
            ("AWAITING_AUTHORIZATION", "test_setup"),
            ("EXECUTING", "test_setup"),
            ("OBSERVING_OUTCOME", "test_setup"),
        ):
            await transition_mission_async(
                session,
                mission_id=mission_id,
                to_state=to_state,
                event_type=event_type,
                actor="test",
                payload={},
                now=now,
            )

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE scheduled_reevaluations SET mission_id = :mid WHERE reevaluation_id = :rid"),
            {"mid": mission_id, "rid": reevaluation_id},
        )
    await engine.dispose()
    return mission_id


async def _mission_event_count(migrated_db: str, mission_id: str) -> int:
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.connect() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM mission_events WHERE mission_id = :mid"), {"mid": mission_id}
            )
        ).scalar_one()
    await engine.dispose()
    return count


@pytest.mark.asyncio
async def test_reclaimed_reevaluation_when_mission_still_waiting_reprocesses_and_completes(
    migrated_db, redis_client, monkeypatch
):
    """
    Crash after claim (simulated: process_payment_failure raises once),
    then a later poll cycle (past the lease) -- standing in for a scheduler
    restart or the next natural POLL_INTERVAL_SECONDS tick, the mechanism is
    identical either way, this is a stateless poller -- reclaims the row.
    Since the mission is still genuinely OBSERVING_OUTCOME (the crash
    happened before any real progress), reprocessing is safe: a new
    diagnosis appears, the mission advances, and the row reaches COMPLETED.
    No orphan survives. Covers: "crash after claim -> eventually
    reprocessed", "scheduler restart with pending/in-flight reevaluation"
    (this poller is stateless -- a fresh run_once() call IS the restart
    case), and "no orphaned reevaluation."
    """
    import workers.retry_scheduler as retry_scheduler_module
    from services.recovery_engine.scheduling import REEVALUATION_LEASE_SECONDS
    from workers.retry_scheduler import run_once

    payment_id, _ = await _seed_high_anomaly_retry_later_fixture(migrated_db)
    result = await decide_and_persist(payment_id, redis_client=redis_client)
    assert result["chosen_action"] == "RETRY_LATER"
    reevaluation_id = result["scheduled_reevaluation_id"]
    mission_id = await _create_mission_in_observing_outcome_and_link(
        migrated_db, payment_id=payment_id, reevaluation_id=reevaluation_id
    )

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        diagnoses_before = (
            await conn.execute(
                text("SELECT count(*) FROM diagnoses WHERE payment_id = :pid"), {"pid": payment_id}
            )
        ).scalar_one()

    # migrated_db is session-scoped -- other test functions in this file may
    # have left their own due/leftover rows in the shared DB. The stub only
    # crashes/counts for THIS test's own payment_id; every other payment
    # passes straight through to the real function, so this test's
    # assertions are never polluted by (and never pollute) any other row
    # run_once() happens to also pick up in the same poll.
    real_process_payment_failure = retry_scheduler_module.process_payment_failure
    call_count = {"n": 0}

    async def _crash_once_then_succeed(pid, *args, **kwargs):
        if pid != payment_id:
            return await real_process_payment_failure(pid, *args, **kwargs)
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated crash mid-reevaluation, after the claim already committed")
        return await real_process_payment_failure(pid, *args, **kwargs)

    monkeypatch.setattr(retry_scheduler_module, "process_payment_failure", _crash_once_then_succeed)

    original_utcnow = clock_module.utcnow
    due_time = datetime.now(UTC) + timedelta(hours=1)
    clock_module.utcnow = lambda: due_time
    try:
        first_poll = await run_once(redis_client)
    finally:
        clock_module.utcnow = original_utcnow
    assert first_poll >= 1
    assert call_count["n"] == 1

    async with engine.begin() as conn:
        fired_row = (
            (
                await conn.execute(
                    text(
                        "SELECT status, lease_expires_at FROM scheduled_reevaluations "
                        "WHERE reevaluation_id = :rid"
                    ),
                    {"rid": reevaluation_id},
                )
            )
            .mappings()
            .first()
        )
    assert fired_row["status"] == "FIRED", "claimed but crashed -- must still be FIRED, not orphan-lost"
    assert fired_row["lease_expires_at"] is not None

    # Second poll, past the lease -- this is the reclaim (and stands in for
    # "the scheduler process restarted and is polling fresh").
    reclaim_time = due_time + timedelta(seconds=REEVALUATION_LEASE_SECONDS + 5)
    clock_module.utcnow = lambda: reclaim_time
    try:
        second_poll = await run_once(redis_client)
    finally:
        clock_module.utcnow = original_utcnow
    assert second_poll >= 1, "the lease-expired row must be picked up again by the next poll"
    assert call_count["n"] == 2, "process_payment_failure must have been called again on reclaim"

    async with engine.begin() as conn:
        final_row = (
            (
                await conn.execute(
                    text("SELECT status FROM scheduled_reevaluations WHERE reevaluation_id = :rid"),
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

    assert final_row["status"] == "COMPLETED", (
        f"a successfully reprocessed reclaim must reach the terminal COMPLETED status, "
        f"got {final_row['status']!r} -- no orphan should survive"
    )
    assert diagnoses_after > diagnoses_before, "the reclaimed reprocessing must have genuinely re-run"


@pytest.mark.asyncio
async def test_reclaimed_reevaluation_when_mission_already_advanced_is_cancelled_not_reprocessed(
    migrated_db, redis_client, monkeypatch
):
    """
    The safety-critical negative case: the original claimant crashed AFTER
    the mission was already durably advanced past OBSERVING_OUTCOME by some
    other path (simulated directly here; in reality e.g. a webhook's
    reconcile_pending_recovery already resolved it). A naive "just flip
    FIRED back to PENDING and redo it" fix would reprocess this row anyway,
    attempting an OBSERVING_OUTCOME -> INVESTIGATING transition against a
    mission that has already left that state -- a duplicate mission event
    at best, a corrupted state machine at worst. The real fix must detect
    this and cancel instead. Proves: "no duplicate execution after reclaim"
    and "no duplicate mission events after reclaim" -- process_payment_failure
    is never called a second time at all.
    """
    import workers.retry_scheduler as retry_scheduler_module
    from services.recovery_engine.mission import transition_mission_async
    from services.recovery_engine.scheduling import REEVALUATION_LEASE_SECONDS
    from workers.retry_scheduler import run_once

    payment_id, _ = await _seed_high_anomaly_retry_later_fixture(migrated_db)
    result = await decide_and_persist(payment_id, redis_client=redis_client)
    assert result["chosen_action"] == "RETRY_LATER"
    reevaluation_id = result["scheduled_reevaluation_id"]
    mission_id = await _create_mission_in_observing_outcome_and_link(
        migrated_db, payment_id=payment_id, reevaluation_id=reevaluation_id
    )

    # migrated_db is session-scoped -- see the sibling test's identical note.
    real_process_payment_failure = retry_scheduler_module.process_payment_failure
    call_count = {"n": 0}

    async def _always_crash(pid, *args, **kwargs):
        if pid != payment_id:
            return await real_process_payment_failure(pid, *args, **kwargs)
        call_count["n"] += 1
        raise RuntimeError("simulated crash mid-reevaluation, after the claim already committed")

    monkeypatch.setattr(retry_scheduler_module, "process_payment_failure", _always_crash)

    original_utcnow = clock_module.utcnow
    due_time = datetime.now(UTC) + timedelta(hours=1)
    clock_module.utcnow = lambda: due_time
    try:
        await run_once(redis_client)
    finally:
        clock_module.utcnow = original_utcnow
    assert call_count["n"] == 1

    # Simulate "some other path already resolved this mission" while the
    # claim was outstanding -- directly advance it past OBSERVING_OUTCOME.
    from recoveryos.database import get_app_session_factory

    async with get_app_session_factory()() as session:
        await transition_mission_async(
            session,
            mission_id=mission_id,
            to_state="TERMINATED",
            event_type="MISSION_TERMINATED",
            actor="test",
            payload={"reason": "simulated independent resolution while the claim was outstanding"},
            now=datetime.now(UTC),
        )
    events_before_reclaim = await _mission_event_count(migrated_db, mission_id)

    reclaim_time = due_time + timedelta(seconds=REEVALUATION_LEASE_SECONDS + 5)
    clock_module.utcnow = lambda: reclaim_time
    try:
        second_poll = await run_once(redis_client)
    finally:
        clock_module.utcnow = original_utcnow
    assert second_poll >= 1, "the lease-expired row must still be picked up (claim succeeds either way)"

    assert call_count["n"] == 1, (
        "process_payment_failure must NOT be called again -- the mission already moved on, "
        "reprocessing would risk a duplicate/invalid mission event"
    )

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.connect() as conn:
        final_row = (
            (
                await conn.execute(
                    text("SELECT status FROM scheduled_reevaluations WHERE reevaluation_id = :rid"),
                    {"rid": reevaluation_id},
                )
            )
            .mappings()
            .first()
        )
    await engine.dispose()
    assert final_row["status"] == "CANCELLED", (
        f"a reclaim whose mission already moved on must be marked CANCELLED, not reprocessed "
        f"or left orphaned, got {final_row['status']!r}"
    )

    events_after_reclaim = await _mission_event_count(migrated_db, mission_id)
    assert events_after_reclaim == events_before_reclaim, (
        "no new mission_events row may be written by a cancelled (not reprocessed) reclaim"
    )


@pytest.mark.asyncio
async def test_mission_reaches_terminal_state_despite_a_crash_during_replan(
    migrated_db, redis_client, monkeypatch
):
    """
    End-to-end: "no orphaned mission" and "active mission survives worker/
    service restart," traced through to an actual terminal state, not just
    the reevaluation row's own status. A mission stuck in OBSERVING_OUTCOME
    forever (the pre-fix behavior, since its only path forward was the
    now-permanently-orphaned reevaluation) would never reach RECOVERED/
    ESCALATED/TERMINATED. With the lease fix, the crashed round is reclaimed
    and reprocessed, and the mission keeps making progress.
    """
    import workers.retry_scheduler as retry_scheduler_module
    from services.recovery_engine.scheduling import REEVALUATION_LEASE_SECONDS
    from workers.retry_scheduler import run_once

    payment_id, _ = await _seed_high_anomaly_retry_later_fixture(migrated_db)
    result = await decide_and_persist(payment_id, redis_client=redis_client)
    assert result["chosen_action"] == "RETRY_LATER"
    reevaluation_id = result["scheduled_reevaluation_id"]
    mission_id = await _create_mission_in_observing_outcome_and_link(
        migrated_db, payment_id=payment_id, reevaluation_id=reevaluation_id
    )

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.connect() as conn:
        before = (
            await conn.execute(
                text(
                    "SELECT state, current_round FROM recovery_missions WHERE mission_id = :mid"
                ),
                {"mid": mission_id},
            )
        ).first()
    assert before[0] == "OBSERVING_OUTCOME"
    round_before = before[1]

    # migrated_db is session-scoped -- see the sibling tests' identical note.
    real_process_payment_failure = retry_scheduler_module.process_payment_failure
    call_count = {"n": 0}

    async def _crash_once_then_succeed(pid, *args, **kwargs):
        if pid != payment_id:
            return await real_process_payment_failure(pid, *args, **kwargs)
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated crash mid-reevaluation")
        return await real_process_payment_failure(pid, *args, **kwargs)

    monkeypatch.setattr(retry_scheduler_module, "process_payment_failure", _crash_once_then_succeed)

    original_utcnow = clock_module.utcnow
    due_time = datetime.now(UTC) + timedelta(hours=1)
    clock_module.utcnow = lambda: due_time
    try:
        await run_once(redis_client)  # crashes once, row stays FIRED with a lease
        reclaim_time = due_time + timedelta(seconds=REEVALUATION_LEASE_SECONDS + 5)
        clock_module.utcnow = lambda: reclaim_time
        await run_once(redis_client)  # reclaims and successfully reprocesses
    finally:
        clock_module.utcnow = original_utcnow

    async with engine.connect() as conn:
        after = (
            await conn.execute(
                text(
                    "SELECT state, ended_at, current_round FROM recovery_missions WHERE mission_id = :mid"
                ),
                {"mid": mission_id},
            )
        ).first()
        reevaluation_rows_after = (
            await conn.execute(
                text("SELECT count(*) FROM scheduled_reevaluations WHERE mission_id = :mid"),
                {"mid": mission_id},
            )
        ).scalar_one()
    await engine.dispose()

    assert call_count["n"] == 2, "expected exactly one crash then one successful reprocess"
    # The mission must have made REAL progress -- either it left
    # OBSERVING_OUTCOME entirely (recovered/escalated/terminated), or it
    # legitimately cycled back through a NEW round (current_round
    # incremented, a second scheduled_reevaluations row now exists) -- both
    # are genuine forward motion, as opposed to the pre-fix behavior where
    # the mission would stay on the SAME round forever because its only
    # path forward (the original reevaluation) was permanently orphaned.
    made_real_progress = (
        after[0] != "OBSERVING_OUTCOME"
        or after[1] is not None
        or after[2] > round_before
        or reevaluation_rows_after > 1
    )
    assert made_real_progress, (
        f"the mission must have genuinely moved (re-investigated at minimum) after the crashed "
        f"round was reclaimed and reprocessed -- it must never be stuck on the SAME round "
        f"forever, got state={after[0]!r} current_round={after[2]!r} (was {round_before!r}) "
        f"reevaluation_rows_for_this_mission={reevaluation_rows_after}"
    )
