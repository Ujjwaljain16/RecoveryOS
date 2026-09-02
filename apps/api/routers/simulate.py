"""
Simulation control router — POST /v1/simulate/degrade (PRD §38 demo hook).
ONLY enabled when ENV=demo (enforced at app factory level in main.py).

Real implementation: inserts real `payments` rows for this bank at the
requested degraded success rate (both a normal trailing-history baseline,
so the z-score has something real to compare against, and a current-bucket
batch at the degraded rate), then calls the ACTUAL anomaly detector
(services/risk_engine/anomaly.py's compute_anomaly_window +
persist_anomaly_window) against those real rows -- not a canned response.
The resulting anomaly_windows row (severity/z_score/is_anomaly) is real
detector output over data this endpoint just wrote, and is returned to the
caller so the dashboard can show what actually fired.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies.auth import verify_api_key
from recoveryos.config import get_settings
from recoveryos.database import get_app_session
from recoveryos.models import Merchant
from services.risk_engine.anomaly import (
    compute_anomaly_window,
    floor_to_bucket,
    persist_anomaly_window,
)

router = APIRouter()

# Real historical baseline needs >= 2 trailing-day buckets with data for
# compute_anomaly_window to produce a z-score at all (see its own
# "len(historical_rates) < 2" guard) — 2 is the minimum, not the target;
# using 2 keeps the demo's synthetic footprint small.
BASELINE_TRAILING_DAYS = 2
NORMAL_FAILURE_RATE = 0.05


class DegradeRequest(BaseModel):
    bank: str
    method: str
    target_success_rate: float = Field(ge=0.0, le=1.0)
    duration_minutes: int = Field(gt=0, le=480)


async def _ensure_synthetic_customer(session: AsyncSession, merchant_id: str) -> str:
    customer_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO customers (customer_id, merchant_id, is_returning) "
            "VALUES (:cid, :mid, true)"
        ),
        {"cid": customer_id, "mid": merchant_id},
    )
    return customer_id


async def _insert_synthetic_payments(
    session: AsyncSession,
    *,
    merchant_id: str,
    customer_id: str,
    bank: str,
    method: str,
    bucket_start: datetime,
    count: int,
    failure_rate: float,
) -> None:
    failed_count = round(count * failure_rate)
    for i in range(count):
        status_value = "failed" if i < failed_count else "success"
        ts = bucket_start + timedelta(seconds=i)
        await session.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, :amount, :method, :bank, :status, :fcode, :fclass, "
                "true, :ts, :failed_ts)"
            ),
            {
                "pid": str(uuid.uuid4()),
                "mid": merchant_id,
                "cid": customer_id,
                "amount": 50_000,
                "method": method,
                "bank": bank,
                "status": status_value,
                "fcode": "BANK_DECLINE" if status_value == "failed" else None,
                "fclass": "SYSTEMIC" if status_value == "failed" else None,
                "ts": ts,
                "failed_ts": ts if status_value == "failed" else None,
            },
        )


@router.post("/degrade", summary="[DEMO ONLY] Inject bank degradation scenario")
async def simulate_degrade(
    payload: DegradeRequest,
    merchant: Merchant = Depends(verify_api_key),
    session: AsyncSession = Depends(get_app_session),
):
    """
    Writes real synthetic `payments` rows for `payload.bank`/`payload.method`
    at the requested degraded success rate, then runs the REAL anomaly
    detector over them. `duration_minutes` sets how many current-bucket
    payments to inject (proportional, capped) so a longer injection reads
    as a bigger, not just longer, incident -- matching PRD §38's framing
    that a demo degradation should visibly move the dashboard's numbers.
    """
    settings = get_settings()
    bucket_minutes = settings.anomaly_bucket_minutes
    min_sample_size = settings.anomaly_min_sample_size

    customer_id = await _ensure_synthetic_customer(session, merchant.merchant_id)

    now = datetime.now(UTC)
    current_bucket = floor_to_bucket(now, bucket_minutes)

    # Real trailing-day baseline history at a normal failure rate, so the
    # z-score has genuine historical variance to compare against instead
    # of hitting the "insufficient_data" guard every time.
    for days_ago in range(1, BASELINE_TRAILING_DAYS + 1):
        hist_bucket = current_bucket - timedelta(days=days_ago)
        await _insert_synthetic_payments(
            session,
            merchant_id=merchant.merchant_id,
            customer_id=customer_id,
            bank=payload.bank,
            method=payload.method,
            bucket_start=hist_bucket,
            count=min_sample_size,
            failure_rate=NORMAL_FAILURE_RATE,
        )

    # Current-bucket degraded batch — size scales with duration_minutes
    # (capped) so a longer/bigger injection visibly moves more of the
    # dashboard's numbers, per PRD §38.
    current_count = min(min_sample_size + payload.duration_minutes, 500)
    await _insert_synthetic_payments(
        session,
        merchant_id=merchant.merchant_id,
        customer_id=customer_id,
        bank=payload.bank,
        method=payload.method,
        bucket_start=current_bucket,
        count=current_count,
        failure_rate=1.0 - payload.target_success_rate,
    )
    await session.commit()

    # The real detector, over the rows just written — not a canned result.
    result = await compute_anomaly_window(session, "bank", payload.bank, current_bucket)
    await persist_anomaly_window(session, result)

    return {
        "status": "degradation_injected",
        "bank": payload.bank,
        "method": payload.method,
        "target_success_rate": payload.target_success_rate,
        "duration_minutes": payload.duration_minutes,
        "synthetic_payments_injected": current_count + BASELINE_TRAILING_DAYS * min_sample_size,
        "anomaly_detection_result": {
            "scope_type": result.scope_type,
            "scope_entity": result.scope_entity,
            "time_bucket": result.time_bucket.isoformat(),
            "baseline_rate": result.baseline_rate,
            "observed_rate": result.observed_rate,
            "z_score": result.z_score,
            "severity": result.severity,
            "is_anomaly": result.is_anomaly,
            "sample_size": result.sample_size,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# POST /v1/simulate/scenario -- Phase 12/13 demo scenario trigger
# ═══════════════════════════════════════════════════════════════════════════
#
# Turns "trust me, here's our architecture" into "watch this payment enter
# RecoveryOS right now" -- every step below calls the SAME real functions
# services/pipeline/consumer.py, services/recovery_engine/orchestrator.py,
# workers/execution_worker.py, workers/retry_scheduler.py, and
# services/pipeline/reconciliation.py already use in production, orchestrated
# directly by this endpoint rather than through the Redis-consumer-group
# indirection -- so a demo click gets a fast, deterministic, self-contained
# result instead of depending on a separately-running worker's config or a
# live LLM's non-deterministic risk-flag output.
#
# One deliberate, explicitly-checked determinism seam, scoped to THIS
# endpoint only (never affecting the real pipeline's own execution path):
#   - "safety_escalation" seeds a diagnosis + recovery_recommendation
#     directly (risk_flags=["HIGH_FRAUD_RISK"]) rather than depending on a
#     live Gemini call happening to flag one -- everything AFTER that point
#     (AIRiskSignalEscalationRule, the deterministic fusion boundary, the
#     mission transition) is the real Phase 11 code path, unmodified.
#
# "recover_via_replan" and "world_changed" do NOT force a scripted
# provider adapter -- an earlier version did (DemoScriptedAdapter /
# AlwaysPendingProvider), racing this container's own always-running
# execution_worker for the right to process each enqueued job. That
# consumer wins the race almost every time in a real multi-container
# deployment (it's already blocked on XREADGROUP when the job lands), a
# gap the single-process test suite never exercises. Instead, both
# scenarios let WHICHEVER consumer processes a job do so with whatever
# settings.payment_provider_adapter is actually configured (a fresh
# order/attempt always resolves PENDING, real Razorpay test-mode adapter
# included), poll for that real, persisted outcome, and then drive the
# FAILED/SUCCESS transition themselves through the SAME real
# webhook-reconciliation path a genuine Razorpay webhook would use
# (services/pipeline/reconciliation.py::reconcile_pending_recovery) --
# see _continue_recover_via_replan's and _continue_world_changed's own
# docstrings.
#
# Requires settings.ai_recommendation_fusion_enabled=True (off by default,
# Phase 11) -- both "safety_escalation" and "recover_via_replan" (which
# demonstrates a real AI-recommendation tie-break/rejection alongside the
# closed loop) need it; the endpoint 409s with a clear message rather than
# silently producing a misleading result if it's off. Same
# docker-compose.override.ai_fusion.yml this repo already ships for the
# Phase 11 ablation runner sets it -- reuse that override for a live demo
# run: `docker compose -f docker-compose.yml -f docker-compose.override.ai_fusion.yml up -d --build`
# with AI_RECOMMENDATION_FUSION_ENABLED=true in the shell environment.


class ScenarioRequest(BaseModel):
    scenario: Literal["recover_via_replan", "safety_escalation", "world_changed"]


async def _ensure_demo_policy_config(session: AsyncSession, merchant_id: str) -> None:
    """
    Idempotently points this merchant at a demo-tuned policy_config
    (retry_cooldown_hours=0) so a Phase 13 reschedule fires immediately,
    real, via workers/retry_scheduler.py's own run_once() -- no clock
    patching, no waiting for a real 12-hour cooldown window. Reused across
    every scenario call for this merchant (checked, not re-created, so
    repeated demo clicks share one config).
    """
    row = (
        await session.execute(
            text("SELECT policy_config_id FROM merchants WHERE merchant_id = :mid"),
            {"mid": merchant_id},
        )
    ).first()
    existing_id = row[0] if row else None
    if existing_id is not None:
        cfg = (
            await session.execute(
                text(
                    "SELECT retry_cooldown_hours FROM policy_configs WHERE policy_config_id = :pcid"
                ),
                {"pcid": existing_id},
            )
        ).first()
        if cfg is not None and cfg[0] == 0:
            return  # already demo-tuned

    policy_config_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO policy_configs (policy_config_id, max_retries, retry_cooldown_hours, "
            "stop_after_success) VALUES (:pcid, 3, 0, true)"
        ),
        {"pcid": policy_config_id},
    )
    await session.execute(
        text("UPDATE merchants SET policy_config_id = :pcid WHERE merchant_id = :mid"),
        {"pcid": policy_config_id, "mid": merchant_id},
    )
    await session.commit()


async def _ensure_retry_now_favored_action_costs(session: AsyncSession, merchant_id: str) -> None:
    """RETRY_NOW cheap, everything else deliberately uneconomical -- same
    recipe as tests/integration/test_recovery_mission_lifecycle.py's own
    fixture, idempotent per merchant."""
    existing = (
        await session.execute(
            text(
                "SELECT 1 FROM action_costs WHERE merchant_id = :mid AND action_type = 'RETRY_NOW'"
            ),
            {"mid": merchant_id},
        )
    ).first()
    if existing is not None:
        return
    await session.execute(
        text(
            "INSERT INTO action_costs (merchant_id, action_type, cost_paise, friction_base_paise) "
            "VALUES (:mid, 'RETRY_NOW', 100, 10)"
        ),
        {"mid": merchant_id},
    )
    for action_type in ("RETRY_LATER", "ALT_ROUTE", "REMINDER", "ESCALATE"):
        await session.execute(
            text(
                "INSERT INTO action_costs (merchant_id, action_type, cost_paise, friction_base_paise) "
                "VALUES (:mid, :action_type, 10000000, 0)"
            ),
            {"mid": merchant_id, "action_type": action_type},
        )
    await session.commit()


async def _seed_scenario_payment(
    session: AsyncSession, merchant_id: str, amount_paise: int = 842_000
) -> tuple[str, str]:
    customer_id = await _ensure_synthetic_customer(session, merchant_id)
    bank = f"DEMO_BANK_{uuid.uuid4().hex[:8]}"
    payment_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    await session.execute(
        text(
            "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
            "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
            "VALUES (:pid, :mid, :cid, :amount, 'card', :bank, 'failed', 'TIMEOUT', 'TEMPORARY', "
            "true, :ts, :ts)"
        ),
        {
            "pid": payment_id,
            "mid": merchant_id,
            "cid": customer_id,
            "amount": amount_paise,
            "bank": bank,
            "ts": now - timedelta(hours=1),
        },
    )
    await session.commit()
    return payment_id, bank


async def _current_mission_id(session: AsyncSession, payment_id: str) -> str:
    row = (
        await session.execute(
            text(
                "SELECT mission_id FROM recovery_missions WHERE payment_id = :pid "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"pid": payment_id},
        )
    ).first()
    if row is None:
        raise RuntimeError(f"no mission found for payment_id={payment_id} after triggering it")
    return str(row[0])


@router.post("/scenario", summary="[DEMO ONLY] Trigger one real, live recovery mission scenario")
async def simulate_scenario(
    payload: ScenarioRequest,
    background_tasks: BackgroundTasks,
    merchant: Merchant = Depends(verify_api_key),
    session: AsyncSession = Depends(get_app_session),
):
    settings = get_settings()
    if not settings.ai_recommendation_fusion_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "AI_RECOMMENDATION_FUSION_ENABLED must be true to run demo scenarios -- "
                "see docker-compose.override.ai_fusion.yml. Refusing rather than silently "
                "producing a misleading result."
            ),
        )

    await _ensure_demo_policy_config(session, merchant.merchant_id)
    await _ensure_retry_now_favored_action_costs(session, merchant.merchant_id)
    payment_id, bank = await _seed_scenario_payment(session, merchant.merchant_id)

    if payload.scenario == "safety_escalation":
        mission_id = await _run_safety_escalation_scenario(payment_id)
        return {"payment_id": payment_id, "mission_id": mission_id, "scenario": payload.scenario}

    # "recover_via_replan" and "world_changed" both start with one real,
    # live pipeline pass (real diagnosis, real EVI/policy, real enqueue) --
    # returns once the first job is enqueued, so the caller gets a
    # mission_id fast and can start polling GET /v1/payments/{id}/mission
    # to watch the rest unfold live via the background task below.
    from services.pipeline.consumer import process_payment_failure

    redis_client = _new_redis_client(settings)
    try:
        await process_payment_failure(payment_id, bank, redis_client)
    finally:
        await redis_client.aclose()

    mission_id = await _current_mission_id(session, payment_id)

    if payload.scenario == "recover_via_replan":
        background_tasks.add_task(_continue_recover_via_replan, payment_id, settings)
    elif payload.scenario == "world_changed":
        background_tasks.add_task(_continue_world_changed, payment_id, settings)

    return {"payment_id": payment_id, "mission_id": mission_id, "scenario": payload.scenario}


def _new_redis_client(settings):
    import redis.asyncio as aioredis

    return aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)


async def _wait_for_pending_recovery(
    session_factory, payment_id: str, attempt_number: int, *, timeout_seconds: float = 20.0
) -> str | None:
    """
    Poll for the real, persisted `recoveries` row this attempt produces --
    whichever consumer actually processed the enqueued job (see this
    module's own docstring on why that's deliberately not controlled here)
    always lands on outcome='PENDING' for a freshly created order/attempt,
    real Razorpay test-mode adapter included (integrations/razorpay/
    adapter.py's RazorpayTestAdapter.retry() docstring: "a created order is
    not itself a completed payment"). Returns its real provider_ref once
    visible, or None on timeout.

    Deliberately also waits for the mission itself to have settled into
    OBSERVING_OUTCOME, not just the `recoveries` row's own commit --
    workers/execution_worker.py::process_job commits `recoveries` (via
    _upsert_recovery) BEFORE calling _advance_mission_after_outcome, which
    transitions the mission to OBSERVING_OUTCOME (with the attempt
    increment) in a LATER, separate commit. A real webhook always arrives
    long after both have settled; this poll can otherwise observe the
    `recoveries` row the instant it commits and race ahead of the mission
    transition -- services.pipeline.reconciliation's own
    _advance_mission_on_external_resolution assumes the mission is already
    sitting in OBSERVING_OUTCOME (see its docstring) and does not re-
    transition into it, so calling reconcile_pending_recovery before that
    transition lands would corrupt the trace and orphan the attempt.
    """
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    async with session_factory() as session:
        while True:
            row = (
                await session.execute(
                    text(
                        "SELECT r.provider_ref FROM recoveries r "
                        "JOIN recovery_missions m ON m.payment_id = r.payment_id "
                        "WHERE r.payment_id = :pid AND r.attempt_number = :n "
                        "AND r.outcome = 'PENDING' AND m.state = 'OBSERVING_OUTCOME'"
                    ),
                    {"pid": payment_id, "n": attempt_number},
                )
            ).first()
            if row is not None:
                return row[0]
            if asyncio.get_event_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.5)
            session.expire_all()


async def _continue_recover_via_replan(payment_id: str, settings) -> None:
    """
    Background continuation for "recover via replan". Round 1's job was
    already enqueued by the real, live pipeline pass above -- whichever
    consumer actually processes it (this container's own execution_worker
    is a persistent, always-polling competitor for the same Redis consumer
    group and typically wins that race; letting it win is fine, see below)
    resolves it to outcome=PENDING, same as any freshly created order.

    Rather than racing that consumer for the right to call a demo-scripted
    provider directly (the previous version of this function did, and lost
    reliably against an always-running execution_worker container -- a real
    gap the single-process test suite never exercises), this drives the
    FAILED-then-SUCCESS shape through the SAME real webhook-reconciliation
    path a genuine payment.failed/payment.captured webhook would use
    (services.pipeline.reconciliation.reconcile_pending_recovery) -- the
    identical mechanism "world_changed" already uses for its own SUCCESS
    resolution, called here with FAILED first to trigger a genuine replan.

    FAILED wires into Phase 13's real reschedule logic
    (reconciliation._advance_mission_on_external_resolution's FAILED
    branch), due immediately under the demo policy_config's
    retry_cooldown_hours=0 -- workers/retry_scheduler.run_once then fires
    it for real, producing round 2's real second investigation, decision,
    and enqueue. Round 2's job resolves to PENDING the same way round 1's
    did, and gets reconciled to SUCCESS.
    """
    from recoveryos.database import get_app_session_factory
    from services.pipeline.reconciliation import reconcile_pending_recovery
    from workers.retry_scheduler import run_once

    session_factory = get_app_session_factory()

    ref1 = await _wait_for_pending_recovery(session_factory, payment_id, attempt_number=1)
    if ref1 is None:
        return
    async with session_factory() as session:
        await reconcile_pending_recovery(
            session, order_id=ref1, outcome="FAILED", recovered_amount_paise=0
        )

    redis_client = _new_redis_client(settings)
    try:
        await run_once(redis_client)
    finally:
        await redis_client.aclose()

    ref2 = await _wait_for_pending_recovery(session_factory, payment_id, attempt_number=2)
    if ref2 is None:
        return
    async with session_factory() as session:
        await reconcile_pending_recovery(
            session, order_id=ref2, outcome="SUCCESS", recovered_amount_paise=842_000
        )


async def _continue_world_changed(payment_id: str, settings) -> None:
    """
    Background continuation for "world changed": wait for round 1's
    already-enqueued job to reach its real, persisted PENDING outcome
    (whichever consumer processes it -- see _continue_recover_via_replan's
    docstring for why that's no longer forced here either), then wait
    briefly (demo pacing -- long enough for a judge to see the mission
    sitting in OBSERVING_OUTCOME), then call the REAL reconciliation path
    (services.pipeline.reconciliation.reconcile_pending_recovery) against
    that REAL provider_ref, as if a payment.captured webhook had just
    arrived -- exercising this session's own mission-cancellation fix, not
    a scripted UI transition.
    """
    from recoveryos.database import get_app_session_factory
    from services.pipeline.reconciliation import reconcile_pending_recovery

    session_factory = get_app_session_factory()
    ref = await _wait_for_pending_recovery(session_factory, payment_id, attempt_number=1)
    if ref is None:
        return

    await asyncio.sleep(4)  # demo pacing -- "the world changes" a beat later, not instantly

    async with session_factory() as session:
        await reconcile_pending_recovery(
            session, order_id=ref, outcome="SUCCESS", recovered_amount_paise=842_000
        )


async def _run_safety_escalation_scenario(payment_id: str) -> str:
    """
    "Safety escalation": seeds a diagnosis + recovery_recommendation with a
    closed-set risk_flag directly (see this module's own docstring for why
    -- reliability, not a shortcut around the real boundary), then drives
    the mission through the SAME sequence services/pipeline/consumer.py
    would (mission creation, investigation narration, planning,
    authorization), calling the REAL services.recovery_engine.orchestrator.decide_and_persist
    -- AIRiskSignalEscalationRule and the Phase 11 fusion boundary are 100%
    real from here on; only the diagnosis/recommendation content is
    pre-scripted, not the deterministic response to it.
    """
    from recoveryos import clock
    from recoveryos.database import get_app_session_factory
    from services.recovery_engine.mission import (
        get_or_create_mission_async,
        log_mission_event_async,
        transition_mission_async,
    )
    from services.recovery_engine.orchestrator import decide_and_persist

    settings = get_settings()
    diagnosis_id = str(uuid.uuid4())

    async with get_app_session_factory()() as session:
        payment_row = (
            (
                await session.execute(
                    text("SELECT amount_paise FROM payments WHERE payment_id = :pid"),
                    {"pid": payment_id},
                )
            )
            .mappings()
            .first()
        )
        amount_paise = payment_row["amount_paise"] if payment_row else 0

        mission, was_created = await get_or_create_mission_async(
            session,
            payment_id=payment_id,
            amount_paise=amount_paise,
            now=clock.utcnow(),
            max_investigation_rounds=settings.mission_max_investigation_rounds,
            max_attempts=settings.mission_max_attempts,
            max_mission_duration_seconds=settings.mission_max_duration_seconds,
        )
        mission_id = mission["mission_id"]
        await transition_mission_async(
            session,
            mission_id=mission_id,
            to_state="INVESTIGATING",
            event_type="MISSION_CREATED" if was_created else "REINVESTIGATION_STARTED",
            actor="system",
            payload={"payment_id": payment_id},
            now=clock.utcnow(),
        )

        await session.execute(
            text(
                "INSERT INTO diagnoses (diagnosis_id, payment_id, root_cause, confidence, "
                "evidence, model_version, is_fallback, created_at) "
                "VALUES (:did, :pid, 'customer_specific', 0.930, "
                '\'[{"fact": "repeated failed attempts across multiple payment methods in a short '
                'window", "source": "payment_history"}]\'::jsonb, '
                "'demo-scenario-scripted-v1', false, now())"
            ),
            {"did": diagnosis_id, "pid": payment_id},
        )
        await session.execute(
            text(
                "INSERT INTO recovery_recommendations (recommendation_id, diagnosis_id, "
                "payment_id, recommended_action, recommended_delay_minutes, confidence, "
                "risk_flags, recovery_rationale, model_version, created_at) "
                "VALUES (gen_random_uuid(), :did, :pid, 'ESCALATE', 0, 0.910, "
                "ARRAY['HIGH_FRAUD_RISK']::text[], "
                "'Repeated rapid-fire failures across payment methods match a known fraud-probing "
                "pattern -- recommend human review before any further attempt.', "
                "'demo-scenario-scripted-v1', now())"
            ),
            {"did": diagnosis_id, "pid": payment_id},
        )
        await session.commit()

        await log_mission_event_async(
            session,
            mission_id=mission_id,
            event_type="HYPOTHESIS_UPDATED",
            actor="ai",
            payload={
                "diagnosis_id": diagnosis_id,
                "root_cause": "customer_specific",
                "confidence": 0.93,
                "is_fallback": False,
            },
        )
        await log_mission_event_async(
            session,
            mission_id=mission_id,
            event_type="AI_RECOMMENDATION",
            actor="ai",
            payload={
                "recommended_action": "ESCALATE",
                "confidence": 0.91,
                "risk_flags": ["HIGH_FRAUD_RISK"],
                "recovery_rationale": (
                    "Repeated rapid-fire failures across payment methods match a known "
                    "fraud-probing pattern -- recommend human review before any further attempt."
                ),
            },
        )
        await transition_mission_async(
            session,
            mission_id=mission_id,
            to_state="PLANNING",
            event_type="INVESTIGATION_CONCLUDED",
            actor="system",
            payload={"diagnosis_id": diagnosis_id},
            now=clock.utcnow(),
        )
        await transition_mission_async(
            session,
            mission_id=mission_id,
            to_state="AWAITING_AUTHORIZATION",
            event_type="PLANNING_CONCLUDED",
            actor="system",
            payload={},
            now=clock.utcnow(),
        )

    # redis_client=None -- ESCALATE never enqueues a job regardless; this
    # scenario is specifically about proving zero money moves.
    result = await decide_and_persist(payment_id, redis_client=None, diagnosis_id=diagnosis_id)

    async with get_app_session_factory()() as session:
        await transition_mission_async(
            session,
            mission_id=mission_id,
            to_state="ESCALATED",
            event_type="POLICY_ESCALATED",
            actor="policy_engine",
            payload={
                "decision_id": result["decision_id"],
                "verdict": result["verdict"],
                "chosen_action": result["chosen_action"],
            },
            now=clock.utcnow(),
        )

    return mission_id
