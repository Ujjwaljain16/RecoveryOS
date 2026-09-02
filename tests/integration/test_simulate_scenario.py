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
configured, then drive FAILED/SUCCESS through the real
reconcile_pending_recovery path. This test's own ASGITransport-based client
has no real execution_worker container running alongside it, so
_run_fake_execution_worker below stands in for one -- polling
stream:recovery_jobs and processing whatever lands there with an injected
stub provider, concurrently with the request (an asyncio.Task, not a
sequential step), exactly mirroring how the real container behaves.
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
    "a created order is not itself a completed payment"). Both
    "recover_via_replan" and "world_changed" drive their own FAILED/SUCCESS
    transitions afterward via reconcile_pending_recovery, independent of
    what actually processed the job -- see this module's own docstring."""

    def retry(self, conn, payment_id, amount_paise, attempt_number):
        from integrations.razorpay.adapter import ProviderResult

        return ProviderResult(
            outcome="PENDING",
            provider_ref=f"fake_worker_{uuid.uuid4().hex[:12]}",
            recovered_amount_paise=0,
        )


async def _run_fake_execution_worker(migrated_db: str, redis_client) -> None:
    """Runs until cancelled -- start as an asyncio.Task alongside the
    triggering POST (NOT awaited sequentially after it: the POST itself
    blocks until its BackgroundTasks continuation fully completes, which is
    exactly what's waiting on this loop to process jobs in the first
    place), and cancel it once the test is done with it."""
    from workers.execution_worker import process_job

    engine = create_engine(migrated_db, pool_pre_ping=True)
    seen_ids: set[str] = set()
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


@pytest.mark.asyncio
async def test_recover_via_replan_scenario_closes_the_loop(migrated_db, redis_client):
    """The real demo money shot, hit over HTTP: a payment fails, the real
    pipeline investigates and authorizes RETRY_NOW, round 1 resolves PENDING
    (via _run_fake_execution_worker standing in for the real, always-running
    execution_worker container -- see this module's own docstring),
    apps/api/routers/simulate.py's background continuation reconciles it to
    FAILED for real (the same path a genuine payment.failed webhook would
    take), Phase 13 reschedules for real (cooldown=0 via the demo
    policy_config), the scheduler fires it live, round 2 resolves PENDING
    and gets reconciled to SUCCESS. Same trajectory
    tests/integration/test_recovery_mission_lifecycle.py already proves at
    the function level -- this proves the HTTP trigger + background-task
    continuation wiring around it.

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

    original_utcnow = clock_module.utcnow
    clock_module.utcnow = lambda: datetime.now(UTC)

    client = await _demo_client(ai_recommendation_fusion_enabled=True)
    worker_task = asyncio.create_task(_run_fake_execution_worker(migrated_db, redis_client))
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

        # Exactly ONE mission for this payment -- proving the background
        # continuation's REINVESTIGATION_STARTED reuse worked (Phase 12's
        # "was_created alone decides" logic), not a spurious second mission.
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
            "OUTCOME_PENDING",
            "EXTERNAL_RESOLUTION",
            "REINVESTIGATION_STARTED",
            "HYPOTHESIS_UPDATED",
            "INVESTIGATION_CONCLUDED",
            "PLANNING_CONCLUDED",
            "POLICY_AUTHORIZED",
            "OUTCOME_PENDING",
            "EXTERNAL_RESOLUTION",
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
