"""
POST /v1/simulate/scenario -- the live demo trigger. Real Postgres + real
Redis, zero mocks beyond the one demo-only determinism seam documented in
apps/api/routers/simulate.py's own module docstring (a scripted diagnosis
for safety_escalation).

Builds its own app/client per test (not the shared app/async_client
fixtures) with ENV forced to "demo" BEFORE create_app() runs -- the shared
fixtures resolve before a test body's own monkeypatch/env changes take
effect (pytest resolves fixtures first), and tests/integration/conftest.py's
own redis_client fixture already forces ENV=test for the rest of the suite.
Same workaround tests/integration/test_dashboard_e2e.py's api_server fixture
already uses for the exact same reason (mounting /v1/simulate/degrade).

"recover_via_replan" and "world_changed" no longer force a scripted
provider adapter (apps/api/routers/simulate.py's own module docstring
explains why -- it used to race this SAME process's execution_worker
container in a real deployment and lose). Instead they let whichever
consumer processes an enqueued job do so with whatever provider is
configured. "world_changed" then drives its own SUCCESS transition through
the real reconcile_pending_recovery path (a real webhook stand-in);
"recover_via_replan" drives nothing itself -- it relies entirely on the
REAL Phase 13 closed loop (workers/execution_worker.py's
_advance_mission_after_outcome + workers/retry_scheduler.py) reacting to
whatever outcome the provider actually returns, same as production. This
test's own ASGITransport-based client has no real execution_worker or
retry_scheduler container running alongside it, so _run_fake_execution_worker
below stands in for the former (polling stream:recovery_jobs and processing
whatever lands there with an injected stub provider, concurrently with the
request -- an asyncio.Task, not a sequential step) and an explicit
workers.retry_scheduler.run_once(...) call stands in for the latter's next
poll cycle, exactly mirroring how the real containers behave.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

from apps.api.dependencies.auth import generate_api_key
from tests.integration.conftest import seed_merchant_with_api_key


class _AlwaysPending:
    """Stub PaymentProvider for _run_fake_execution_worker: every attempt
    resolves PENDING, the same shape a fresh real order/attempt always has
    (integrations/razorpay/adapter.py's RazorpayTestAdapter.retry() --
    "a created order is not itself a completed payment"). "world_changed"
    drives its own SUCCESS transition afterward via
    reconcile_pending_recovery, independent of what actually processed the
    job -- see this module's own docstring."""

    def retry(self, conn, payment_id, amount_paise, attempt_number):
        from integrations.razorpay.adapter import ProviderResult

        return ProviderResult(
            outcome="PENDING",
            provider_ref=f"fake_worker_{uuid.uuid4().hex[:12]}",
            recovered_amount_paise=0,
        )


class _FailsOnceThenSucceeds:
    """Stub PaymentProvider for "recover_via_replan": resolves attempt 1
    straight to FAILED and attempt 2+ straight to SUCCESS -- no PENDING
    window, the same immediate-outcome shape the REAL default
    SimulatorAdapter.retry() uses once a simulator_latent_state row exists
    (integrations/razorpay/adapter.py). Mirrors apps/api/routers/
    simulate.py's own true_recovery_prob_bps=0 seed for this scenario
    (attempt 1 is genuinely, deterministically forced to fail there too --
    this stub isn't inventing new behavior, just avoiding this test's
    dependence on the real, only-probably-favorable attempt-2 dice roll a
    live demo run accepts)."""

    def retry(self, conn, payment_id, amount_paise, attempt_number):
        from integrations.razorpay.adapter import ProviderResult

        if attempt_number <= 1:
            return ProviderResult(
                outcome="FAILED",
                provider_ref=f"fake_worker_{uuid.uuid4().hex[:12]}",
                recovered_amount_paise=0,
            )
        return ProviderResult(
            outcome="SUCCESS",
            provider_ref=f"fake_worker_{uuid.uuid4().hex[:12]}",
            recovered_amount_paise=amount_paise,
        )


async def _run_fake_execution_worker(migrated_db: str, redis_client, provider=None) -> None:
    """Runs until cancelled -- start as an asyncio.Task alongside the
    triggering POST. For "world_changed" the POST itself blocks until its
    BackgroundTasks continuation fully completes, which is exactly what's
    waiting on this loop to process jobs in the first place. "recover_via_
    replan" no longer has a BackgroundTasks continuation at all (see
    apps/api/routers/simulate.py's own module docstring) -- its caller
    polls for this loop's real, persisted effects instead. Cancel this task
    once the test is done with it either way."""
    from workers.execution_worker import process_job

    engine = create_engine(migrated_db, pool_pre_ping=True)
    seen_ids: set[str] = set()
    if provider is None:
        provider = _AlwaysPending()
    try:
        while True:
            entries = await redis_client.xrange("stream:recovery_jobs", min="-", max="+")
            for entry_id, fields in entries:
                if entry_id in seen_ids:
                    continue
                seen_ids.add(entry_id)
                with engine.connect() as conn:
                    process_job(conn, fields, provider=provider)
            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        pass
    finally:
        engine.dispose()


async def _demo_client(*, ai_recommendation_fusion_enabled: bool) -> AsyncClient:
    os.environ["ENV"] = "demo"
    os.environ["AI_RECOMMENDATION_FUSION_ENABLED"] = (
        "true" if ai_recommendation_fusion_enabled else "false"
    )
    # tests/conftest.py's session-wide _pinned_clock_for_determinism patches
    # recoveryos.clock.utcnow() (the async side, e.g.
    # services/recovery_engine/mission.py's get_or_create_mission_async) to
    # a fixed 2026-08-25, while workers/execution_worker.py's mission-budget
    # check deliberately uses real datetime.now(UTC) (see that module's own
    # docstring for why) -- a gap that only exists under this pinned-clock
    # test fixture, never in a real running server where both sides read
    # real wall-clock time. A generous override sidesteps the test-only
    # artifact without weakening the real default.
    os.environ["MISSION_MAX_DURATION_SECONDS"] = str(365 * 24 * 3600)
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    from apps.api.main import create_app

    app = create_app()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seeded_merchant(
    migrated_db: str, name: str = "scenario-test-merchant"
) -> tuple[str, str]:
    merchant_id = str(uuid.uuid4())
    raw_key = generate_api_key()
    await seed_merchant_with_api_key(migrated_db, merchant_id, name, raw_key)
    return merchant_id, raw_key


def _mission_state(migrated_db: str, payment_id: str) -> dict | None:
    engine = create_engine(migrated_db, pool_pre_ping=True)
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT mission_id, state, current_attempt, current_round FROM recovery_missions "
                    "WHERE payment_id = :pid ORDER BY created_at DESC LIMIT 1"
                ),
                {"pid": payment_id},
            )
            .mappings()
            .first()
        )
    engine.dispose()
    return dict(row) if row else None


def _mission_events(migrated_db: str, payment_id: str) -> list[tuple]:
    engine = create_engine(migrated_db, pool_pre_ping=True)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT me.event_type, me.actor FROM mission_events me "
                "JOIN recovery_missions m ON m.mission_id = me.mission_id "
                "WHERE m.payment_id = :pid ORDER BY me.sequence_number"
            ),
            {"pid": payment_id},
        ).fetchall()
    engine.dispose()
    return [tuple(r) for r in rows]


@pytest.mark.asyncio
async def test_scenario_refuses_when_fusion_disabled(migrated_db):
    from recoveryos.config import get_settings

    client = await _demo_client(ai_recommendation_fusion_enabled=False)
    try:
        _merchant_id, api_key = await _seeded_merchant(migrated_db)
        resp = await client.post(
            "/v1/simulate/scenario",
            json={"scenario": "safety_escalation"},
            headers={"X-API-Key": api_key},
        )
        assert resp.status_code == 409
    finally:
        await client.aclose()
        get_settings.cache_clear()


async def _poll_until(predicate, *, timeout: float = 10.0, interval: float = 0.2) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        if predicate():
            return True
        if asyncio.get_event_loop().time() >= deadline:
            return False
        await asyncio.sleep(interval)


@pytest.mark.asyncio
async def test_recover_via_replan_scenario_closes_the_loop(migrated_db, redis_client):
    """The real demo money shot, hit over HTTP: a payment fails, the real
    pipeline investigates and authorizes RETRY_NOW, round 1 resolves
    straight to FAILED (via _run_fake_execution_worker standing in for the
    real, always-running execution_worker container, using
    _FailsOnceThenSucceeds -- the same immediate-outcome shape the real
    default SimulatorAdapter uses, no PENDING window -- see this module's
    own docstring). workers/execution_worker.py's own real Phase 13 code
    (_advance_mission_after_outcome's FAILED branch) reschedules a
    re-evaluation for real (cooldown=0 via the demo policy_config, due
    immediately); this test then calls workers/retry_scheduler.run_once
    directly, standing in for the real, always-running retry_scheduler
    container's own poll loop the same way _run_fake_execution_worker
    stands in for execution_worker's -- apps/api/routers/simulate.py no
    longer drives any of this itself for recover_via_replan (see its own
    module docstring for why: SimulatorAdapter never produces the PENDING
    window the old scripted continuation depended on). Round 2 resolves
    straight to SUCCESS the same way. Same trajectory
    tests/integration/test_recovery_mission_lifecycle.py already proves at
    the function level -- this proves the HTTP trigger + real closed-loop
    wiring around it, end to end.

    workers/execution_worker.py computes scheduled_for from REAL wall-clock
    time (datetime.now(UTC), by design), while workers/retry_scheduler.py's
    "is it due" check reads recoveryos.clock.utcnow() -- pinned to a fixed
    2026-08-25 by tests/conftest.py's session-wide fixture, which makes
    ANY real-time-computed scheduled_for look like it's still in the
    future from the pinned clock's perspective. Only a test-environment
    artifact (a real running server has both sides reading real time) --
    patch the clock seam to real time for this test's duration so the two
    genuinely align, the same way test_retry_scheduler.py's own tests
    manipulate this exact seam to simulate time passing.
    """
    from datetime import UTC, datetime

    import recoveryos.clock as clock_module
    from recoveryos.config import get_settings
    from workers.retry_scheduler import run_once

    original_utcnow = clock_module.utcnow
    clock_module.utcnow = lambda: datetime.now(UTC)

    client = await _demo_client(ai_recommendation_fusion_enabled=True)
    worker_task = asyncio.create_task(
        _run_fake_execution_worker(migrated_db, redis_client, provider=_FailsOnceThenSucceeds())
    )
    try:
        _merchant_id, api_key = await _seeded_merchant(migrated_db)
        resp = await client.post(
            "/v1/simulate/scenario",
            json={"scenario": "recover_via_replan"},
            headers={"X-API-Key": api_key},
        )
        assert resp.status_code == 200
        body = resp.json()
        payment_id = body["payment_id"]
        mission_id = body["mission_id"]

        # Round 1: wait for the fake execution_worker to process the
        # already-enqueued job and land the real FAILED-branch event.
        settled_round_1 = await _poll_until(
            lambda: "RECOVERY_FAILED" in [e[0] for e in _mission_events(migrated_db, payment_id)]
        )
        assert settled_round_1, "round 1 never resolved to RECOVERY_FAILED in time"

        # Stand in for the real, always-running retry_scheduler container's
        # next poll cycle -- picks up the reschedule row schedule_reevaluation_sync
        # just wrote (due immediately, cooldown=0), re-runs a real
        # investigation/decision, and enqueues round 2's job for real.
        #
        # Not asserted as == 1: this shared testcontainers Postgres can carry
        # OTHER tests' own scheduled_reevaluations rows too (real rows this
        # test doesn't own), and patching clock.utcnow() to real time above
        # makes any of THEIRS that are already past-due become due here as
        # well -- harmless (_process_one safely CANCELs any whose mission has
        # already moved past OBSERVING_OUTCOME), but means the exact count
        # isn't this test's own to assert. Confirmed CI-only (full-suite,
        # shared DB): processed == 3 there, == 1 run in isolation locally.
        # This test's own correctness is verified below by payment_id's own
        # mission actually reaching RECOVERED with the right event sequence.
        processed = await run_once(redis_client)
        assert processed >= 1

        # Round 2: wait for the fake execution_worker to process THAT job
        # and land the mission in its final RECOVERED state.
        settled_round_2 = await _poll_until(
            lambda: (_mission_state(migrated_db, payment_id) or {}).get("state") == "RECOVERED"
        )
        assert settled_round_2, "round 2 never resolved to RECOVERED in time"

        # Exactly ONE mission for this payment -- proving the real
        # REINVESTIGATION_STARTED reuse worked (Phase 12's "was_created
        # alone decides" logic), not a spurious second mission.
        engine = create_engine(migrated_db, pool_pre_ping=True)
        with engine.connect() as conn:
            mission_count = conn.execute(
                text("SELECT count(*) FROM recovery_missions WHERE payment_id = :pid"),
                {"pid": payment_id},
            ).scalar_one()
        engine.dispose()
        assert mission_count == 1

        mission = _mission_state(migrated_db, payment_id)
        assert mission is not None
        assert str(mission["mission_id"]) == mission_id
        assert mission["state"] == "RECOVERED"
        assert mission["current_attempt"] == 2
        assert mission["current_round"] == 1

        events = _mission_events(migrated_db, payment_id)
        event_types = [e[0] for e in events]
        assert event_types == [
            "MISSION_CREATED",
            "HYPOTHESIS_UPDATED",
            "INVESTIGATION_CONCLUDED",
            "PLANNING_CONCLUDED",
            "POLICY_AUTHORIZED",
            "RECOVERY_FAILED",
            "REINVESTIGATION_STARTED",
            "HYPOTHESIS_UPDATED",
            "INVESTIGATION_CONCLUDED",
            "PLANNING_CONCLUDED",
            "POLICY_AUTHORIZED",
            "RECOVERY_SUCCEEDED",
            "MISSION_RECOVERED",
        ]

        engine = create_engine(migrated_db, pool_pre_ping=True)
        with engine.connect() as conn:
            recovered_row = conn.execute(
                text("SELECT actual_recovery_paise FROM recovery_ledger WHERE payment_id = :pid"),
                {"pid": payment_id},
            ).first()
        engine.dispose()
        assert recovered_row is not None
        assert recovered_row[0] == 842_000
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        await client.aclose()
        get_settings.cache_clear()
        clock_module.utcnow = original_utcnow


@pytest.mark.asyncio
async def test_world_changed_scenario_closes_mission_via_external_resolution(
    migrated_db, redis_client
):
    """A payment's order is created (PENDING, awaiting payment, via
    _run_fake_execution_worker standing in for the real, always-running
    execution_worker container -- see this module's own docstring) and the
    mission sits in OBSERVING_OUTCOME -- then the REAL reconciliation path
    this session's own correctness fix wired up
    (services/pipeline/reconciliation.py's _advance_mission_on_external_resolution)
    closes the mission, as if a real payment.captured webhook had just
    arrived. Takes >= 4s for real (the background task's own demo pacing
    sleep) -- not shortened, since that pacing IS part of what's being
    proven to actually run."""
    from recoveryos.config import get_settings

    client = await _demo_client(ai_recommendation_fusion_enabled=True)
    worker_task = asyncio.create_task(_run_fake_execution_worker(migrated_db, redis_client))
    try:
        _merchant_id, api_key = await _seeded_merchant(migrated_db)
        resp = await client.post(
            "/v1/simulate/scenario",
            json={"scenario": "world_changed"},
            headers={"X-API-Key": api_key},
        )
        assert resp.status_code == 200
        body = resp.json()
        payment_id = body["payment_id"]
        mission_id = body["mission_id"]

        mission = _mission_state(migrated_db, payment_id)
        assert mission is not None
        assert str(mission["mission_id"]) == mission_id
        assert mission["state"] == "RECOVERED"
        assert mission["current_attempt"] == 1

        events = _mission_events(migrated_db, payment_id)
        event_types = [e[0] for e in events]
        assert event_types == [
            "MISSION_CREATED",
            "HYPOTHESIS_UPDATED",
            "INVESTIGATION_CONCLUDED",
            "PLANNING_CONCLUDED",
            "POLICY_AUTHORIZED",
            "OUTCOME_PENDING",  # execution_worker.py's own PENDING-outcome narration
            "EXTERNAL_RESOLUTION",
            "MISSION_RECOVERED",
        ]

        engine = create_engine(migrated_db, pool_pre_ping=True)
        with engine.connect() as conn:
            recovery_row = (
                conn.execute(
                    text(
                        "SELECT outcome, recovered_amount_paise FROM recoveries WHERE payment_id = :pid"
                    ),
                    {"pid": payment_id},
                )
                .mappings()
                .first()
            )
        engine.dispose()
        assert recovery_row is not None
        assert recovery_row["outcome"] == "SUCCESS"
        assert recovery_row["recovered_amount_paise"] == 842_000
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        await client.aclose()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_safety_escalation_scenario_escalates_with_zero_money_moved(migrated_db):
    from recoveryos.config import get_settings

    client = await _demo_client(ai_recommendation_fusion_enabled=True)
    try:
        _merchant_id, api_key = await _seeded_merchant(migrated_db)
        resp = await client.post(
            "/v1/simulate/scenario",
            json={"scenario": "safety_escalation"},
            headers={"X-API-Key": api_key},
        )
        assert resp.status_code == 200
        body = resp.json()
        payment_id = body["payment_id"]
        assert body["scenario"] == "safety_escalation"

        mission = _mission_state(migrated_db, payment_id)
        assert mission is not None
        assert mission["state"] == "ESCALATED"

        engine = create_engine(migrated_db, pool_pre_ping=True)
        with engine.connect() as conn:
            recoveries_count = conn.execute(
                text("SELECT count(*) FROM recoveries WHERE payment_id = :pid"), {"pid": payment_id}
            ).scalar_one()
        engine.dispose()
        assert recoveries_count == 0, "ESCALATE must never move money"

        event_types = [e[0] for e in _mission_events(migrated_db, payment_id)]
        assert "MISSION_CREATED" in event_types
        assert "AI_RECOMMENDATION" in event_types
        assert event_types[-1] == "POLICY_ESCALATED"
    finally:
        await client.aclose()
        get_settings.cache_clear()
