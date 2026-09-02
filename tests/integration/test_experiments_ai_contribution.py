"""
GET /v1/experiments/live's ai_contribution block -- real counts from
decision_fusion_trace, mirroring tests/evaluation/ai_ablation_runner.py's
own collect_metrics() queries, scoped to one merchant. Same
generate_candidate_actions-monkeypatch isolation pattern as
tests/integration/test_ai_recommendation_bounded_influence.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import services.recovery_engine.orchestrator as orchestrator_module
from apps.api.dependencies.auth import generate_api_key
from services.recovery_engine.next_best_action import CandidateActionResult
from services.recovery_engine.orchestrator import build_decision, persist_decision
from tests.integration.conftest import seed_merchant_with_api_key, to_async_url


def _pg_text_array(values: list[str]) -> str:
    if not values:
        return "'{}'::text[]"
    return "ARRAY[" + ",".join(repr(v) for v in values) + "]::text[]"


async def _insert_payment_for_merchant(
    migrated_db: str, merchant_id: str, *, amount_paise: int = 200_000
) -> str:
    customer_id = str(uuid.uuid4())
    payment_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO customers (customer_id, merchant_id, is_returning, "
                "lifetime_value_paise) VALUES (:cid, :mid, false, 0)"
            ),
            {"cid": customer_id, "mid": merchant_id},
        )
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, :amount, 'card', 'HDFC', 'failed', 'TIMEOUT', "
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


async def _insert_diagnosis_and_recommendation(
    migrated_db: str,
    *,
    payment_id: str,
    recommended_action: str,
    confidence: float,
    risk_flags: list[str] | None = None,
) -> str:
    diagnosis_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO diagnoses (diagnosis_id, payment_id, root_cause, confidence, "
                "evidence, model_version, is_fallback, created_at) "
                "VALUES (:did, :pid, 'unknown', 0.5, '[]'::jsonb, 'test-v1', false, now())"
            ),
            {"did": diagnosis_id, "pid": payment_id},
        )
        await conn.execute(
            text(
                "INSERT INTO recovery_recommendations (recommendation_id, diagnosis_id, "
                "payment_id, recommended_action, recommended_delay_minutes, confidence, "
                f"risk_flags, recovery_rationale, model_version, created_at) "
                f"VALUES (gen_random_uuid(), :did, :pid, :action, 0, :conf, "
                f"{_pg_text_array(risk_flags or [])}, 'test rationale', 'test-v1', now())"
            ),
            {
                "did": diagnosis_id,
                "pid": payment_id,
                "action": recommended_action,
                "conf": confidence,
            },
        )
    await engine.dispose()
    return diagnosis_id


def _fixed_candidates(evi_by_action: dict[str, int]):
    action_types = ("RETRY_NOW", "RETRY_LATER", "ALT_ROUTE", "REMINDER", "ESCALATE", "DO_NOTHING")

    async def _fake_generate_candidate_actions(
        session,
        merchant_id,
        amount_paise,
        customer_is_returning,
        base_propensity_prob_bps,
        anomaly_context,
    ):
        return tuple(
            CandidateActionResult(
                action_type=a,
                recovery_prob_bps=5000,
                expected_value_paise=evi_by_action.get(a, -999_999),
                cost_paise=0,
                friction_penalty_paise=0,
                risk_penalty_paise=0,
            )
            for a in action_types
        )

    return _fake_generate_candidate_actions


@pytest.mark.asyncio
async def test_ai_contribution_reflects_a_real_tie_break(async_client, migrated_db, monkeypatch):
    monkeypatch.setenv("AI_RECOMMENDATION_FUSION_ENABLED", "true")
    monkeypatch.setenv("AI_TIE_BREAK_TOLERANCE_BPS", "100")
    from recoveryos.config import get_settings

    get_settings.cache_clear()
    try:
        merchant_id = str(uuid.uuid4())
        api_key = generate_api_key()
        await seed_merchant_with_api_key(migrated_db, merchant_id, "ai-contrib-test", api_key)

        payment_id = await _insert_payment_for_merchant(migrated_db, merchant_id)
        diagnosis_id = await _insert_diagnosis_and_recommendation(
            migrated_db, payment_id=payment_id, recommended_action="ALT_ROUTE", confidence=0.9
        )
        monkeypatch.setattr(
            orchestrator_module,
            "generate_candidate_actions",
            _fixed_candidates({"RETRY_NOW": 8_200, "ALT_ROUTE": 8_170, "REMINDER": 1_000}),
        )

        nba_result, decision, context = await build_decision(payment_id, diagnosis_id=diagnosis_id)
        assert nba_result.chosen_action == "ALT_ROUTE"  # sanity: the tie-break actually applied
        await persist_decision(payment_id, nba_result, decision, context)

        resp = await async_client.get("/v1/experiments/live", headers={"X-API-Key": api_key})
        assert resp.status_code == 200
        body = resp.json()
        ai = body["ai_contribution"]
        assert ai["recommendations_available"] == 1
        assert ai["near_tie_decisions"] == 1
        assert ai["ai_tie_breaks"] == 1
        assert ai["risk_escalations"] == 0
        assert ai["ai_outcome_delta_total"] == 1
        assert ai["ai_outcome_delta_rate"] == 1.0
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_ai_contribution_is_zeroed_for_a_fresh_merchant(async_client, migrated_db):
    merchant_id = str(uuid.uuid4())
    api_key = generate_api_key()
    await seed_merchant_with_api_key(migrated_db, merchant_id, "ai-contrib-fresh", api_key)

    resp = await async_client.get("/v1/experiments/live", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    ai = resp.json()["ai_contribution"]
    assert ai == {
        "recommendations_available": 0,
        "near_tie_decisions": 0,
        "ai_tie_breaks": 0,
        "risk_escalations": 0,
        "ai_outcome_delta_total": 0,
        "ai_outcome_delta_rate": None,
    }
