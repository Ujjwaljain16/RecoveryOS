"""
Adversarial sweep scenario #31 -- AI recommendation replay / stale
recommendation.

services/recovery_engine/orchestrator.py:build_decision(payment_id,
diagnosis_id=...) takes diagnosis_id as a caller-supplied parameter -- it is
never looked up by the orchestrator itself. This test asks: if a caller
replays an OLD diagnosis_id (generated for an earlier round, recommending an
action that was fine THEN) into a build_decision call for a LATER round
where CURRENT policy state would now block that exact action, can the stale
recommendation smuggle it through anyway?

_apply_ai_fusion's tie-break path always re-evaluates the recommended
candidate against the CURRENT PaymentContext/PolicyConfigContext via a fresh
evaluate() call (see services/recovery_engine/orchestrator.py:378) -- this
test proves that re-evaluation is what actually protects a replayed/stale
recommendation, not just decisive-EVI-margin cases already covered by
test_ai_recommendation_bounded_influence.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import services.recovery_engine.orchestrator as orchestrator_module
from services.recovery_engine.next_best_action import CandidateActionResult
from services.recovery_engine.orchestrator import build_decision
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


def _fixed_candidates(evi_by_action: dict[str, int]):
    action_types = ("RETRY_NOW", "RETRY_LATER", "ALT_ROUTE", "REMINDER", "ESCALATE", "DO_NOTHING")

    async def _fake_generate_candidate_actions(
        session, merchant_id, amount_paise, customer_is_returning, base_propensity_prob_bps,
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


def _enable_fusion(monkeypatch, *, tolerance_bps: int = 100):
    monkeypatch.setenv("AI_RECOMMENDATION_FUSION_ENABLED", "true")
    monkeypatch.setenv("AI_TIE_BREAK_TOLERANCE_BPS", str(tolerance_bps))
    from recoveryos.config import get_settings

    get_settings.cache_clear()


async def _insert_payment(
    migrated_db: str, *, amount_paise: int = 200_000, method: str = "card"
) -> str:
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
                "VALUES (:pid, :mid, :cid, :amount, :method, 'HDFC', 'failed', 'TIMEOUT', "
                "'TEMPORARY', true, :ts, :ts)"
            ),
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "amount": amount_paise,
                "method": method,
                "ts": datetime.now(UTC) - timedelta(hours=1),
            },
        )
    await engine.dispose()
    return payment_id


async def _insert_stale_diagnosis_and_recommendation(
    migrated_db: str, *, payment_id: str, recommended_action: str, confidence: float
) -> str:
    """A diagnosis+recommendation as if generated back when the payment
    FIRST failed (before the real attempt-1 above ever ran) -- exactly the
    kind of object a caller might accidentally replay into a later
    build_decision() call for the same payment."""
    diagnosis_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO diagnoses (diagnosis_id, payment_id, root_cause, confidence, "
                "evidence, model_version, is_fallback, created_at) "
                "VALUES (:did, :pid, 'unknown', 0.5, '[]'::jsonb, 'test-v1', false, "
                ":ts)"
            ),
            {"did": diagnosis_id, "pid": payment_id, "ts": datetime.now(UTC) - timedelta(hours=1)},
        )
        await conn.execute(
            text(
                "INSERT INTO recovery_recommendations (recommendation_id, diagnosis_id, "
                "payment_id, recommended_action, recommended_delay_minutes, confidence, "
                "risk_flags, recovery_rationale, model_version, created_at) "
                "VALUES (gen_random_uuid(), :did, :pid, :action, 0, :conf, "
                "'{}'::text[], 'stale rationale', 'test-v1', :ts)"
            ),
            {
                "did": diagnosis_id,
                "pid": payment_id,
                "action": recommended_action,
                "conf": confidence,
                "ts": datetime.now(UTC) - timedelta(hours=1),
            },
        )
    await engine.dispose()
    return diagnosis_id


@pytest.mark.asyncio
async def test_stale_recommendation_recommending_a_policy_blocked_action_is_rejected(
    migrated_db, monkeypatch
):
    """
    diagnosis_id + its recommendation were persisted a full hour before this
    build_decision() call (created_at backdated) -- a genuinely stale object
    being replayed, not one freshly minted for this exact decision. It
    recommends RETRY_NOW for a UPI e-mandate payment whose amount is above
    the RBI AFA-exempt threshold, so RETRY_NOW individually fails
    EMandateRetryComplianceRule on ITS OWN re-evaluation (action-specific --
    unlike CooldownRule/RetryLimitRule, which apply to every candidate
    equally regardless of chosen action, so they can't isolate "the
    recommended action specifically is blocked" from "everything is
    blocked"). The deterministic winner (ALT_ROUTE, which the e-mandate rule
    doesn't apply to) must stand -- proving that being old/replayed changes
    nothing: _apply_ai_fusion always re-evaluates the recommended candidate
    against CURRENT policy state, with no notion of a diagnosis "expiring"
    because none is needed.
    """
    payment_id = await _insert_payment(migrated_db, amount_paise=1_600_000, method="upi")
    diagnosis_id = await _insert_stale_diagnosis_and_recommendation(
        migrated_db, payment_id=payment_id, recommended_action="RETRY_NOW", confidence=0.9
    )

    monkeypatch.setattr(
        orchestrator_module,
        "generate_candidate_actions",
        _fixed_candidates({"ALT_ROUTE": 8_200, "RETRY_NOW": 8_170, "REMINDER": 1_000}),
    )
    _enable_fusion(monkeypatch)

    nba_result, decision, context = await build_decision(payment_id, diagnosis_id=diagnosis_id)

    assert nba_result.chosen_action == "ALT_ROUTE", (
        "the deterministic winner must stand -- a stale, replayed recommendation must not "
        f"win, got {nba_result.chosen_action}"
    )
    assert decision.verdict == "ALLOW"
    assert context["fusion_provenance"]["tie_break_applied"] is False
    assert context["fusion_provenance"]["reject_reason"] == "tie_break_rejected_policy", (
        "the stale RETRY_NOW recommendation must be rejected specifically because it fails "
        f"CURRENT policy re-evaluation, got reject_reason="
        f"{context['fusion_provenance']['reject_reason']!r}"
    )
