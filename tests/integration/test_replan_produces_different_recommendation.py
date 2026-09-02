"""
AI Architecture Gap Audit gap (P2): workers/retry_scheduler.py's
_process_one calls the exact same process_payment_failure ->
diagnose_and_persist as the first decision (no reduced/AI-stripped replan
path exists) -- that wiring is structurally certain, proven by direct code
reading. What was NOT behaviorally demonstrated: that a genuine second round
of investigation, producing a DIFFERENT recommendation than the first,
actually changes the persisted decision.

Proves the causal claim directly rather than routing through the full
mission/scheduler machinery (which would mostly re-test already-covered
scheduler plumbing -- see tests/integration/test_retry_scheduler.py):
diagnose_and_persist() is called twice for the SAME payment with a mocked
LLM returning a different RecoveryRecommendation each time, using a fresh
source_event_id each call -- exactly how retry_scheduler.py really
re-invokes it on a fired reevaluation -- and build_decision()/fusion is
shown producing two different chosen_action results.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import services.recovery_engine.orchestrator as orchestrator_module
from services.diagnosis_engine.diagnoser import diagnose_and_persist
from services.recovery_engine.next_best_action import CandidateActionResult
from services.recovery_engine.orchestrator import build_decision
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


async def _seed_failed_payment(migrated_db: str) -> str:
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    engine = create_async_engine(to_async_url(migrated_db))
    payment_id = str(uuid.uuid4())
    bank = f"TESTBANK_{uuid.uuid4().hex[:8]}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, 200000, 'card', :bank, 'failed', 'TIMEOUT', 'TEMPORARY', "
                "true, now(), now())"
            ),
            {"pid": payment_id, "mid": merchant_id, "cid": customer_id, "bank": bank},
        )
    await engine.dispose()
    return payment_id


def _fixed_candidates():
    """Same near-tied worked example as
    test_ai_recommendation_bounded_influence.py: RETRY_NOW 82.00 is the
    deterministic winner, ALT_ROUTE 81.70 is near-tied (within 1%)."""
    action_types = ("RETRY_NOW", "RETRY_LATER", "ALT_ROUTE", "REMINDER", "ESCALATE", "DO_NOTHING")
    evi_by_action = {"RETRY_NOW": 8_200, "ALT_ROUTE": 8_170, "REMINDER": 1_000, "ESCALATE": 500}

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


def _one_round_response_recommending(action: str, *, confidence: float = 0.9):
    """A single-round-then-finalize investigation ending with a
    recommendation for `action`, well above the confidence floor."""

    async def fake_generate_json(*, system_prompt, user_content, response_schema, model, api_key):
        if "final_hypotheses" in user_content:
            return {
                "selected_cause": "unknown",
                "confidence_band": "LIKELY",
                "evidence": [{"fact": "investigation complete", "source": "system"}],
                "recommended_action": action,
                "recommended_delay_minutes": 0,
                "recommendation_confidence": confidence,
                "risk_flags": [],
                "recovery_rationale": f"recommending {action} this round",
            }
        return {
            "hypotheses": [
                {
                    "cause": "unknown",
                    "support_score": 1,
                    "contradict_score": 0,
                    "unresolved_questions": [],
                }
            ],
            "action": "finalize",
            "reasoning": "no evidence gathering needed for this test",
        }

    return fake_generate_json


def _enable_fusion(monkeypatch, *, tolerance_bps: int = 100):
    monkeypatch.setenv("AI_RECOMMENDATION_FUSION_ENABLED", "true")
    monkeypatch.setenv("AI_TIE_BREAK_TOLERANCE_BPS", str(tolerance_bps))
    monkeypatch.setenv("AI_DIAGNOSER_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-not-real")
    from recoveryos.config import get_settings

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_second_investigation_round_with_a_different_recommendation_changes_the_decision(
    migrated_db, monkeypatch
):
    payment_id = await _seed_failed_payment(migrated_db)
    _enable_fusion(monkeypatch)
    monkeypatch.setattr(orchestrator_module, "generate_candidate_actions", _fixed_candidates())

    # Round 1 ("first decision"): recommends ALT_ROUTE -- near-tied with the
    # RETRY_NOW winner, individually policy-ALLOWED -- must win the tie-break.
    monkeypatch.setattr(
        "services.diagnosis_engine.llm_client.gemini_generate_json",
        _one_round_response_recommending("ALT_ROUTE"),
    )
    diagnosis_1 = await diagnose_and_persist(payment_id, source_event_id=str(uuid.uuid4()))
    assert diagnosis_1 is not None
    nba_1, decision_1, context_1 = await build_decision(
        payment_id, diagnosis_id=diagnosis_1.diagnosis_id
    )
    assert nba_1.chosen_action == "ALT_ROUTE"
    assert context_1["fusion_provenance"]["tie_break_applied"] is True

    # Round 2 ("the replan"): a genuinely fresh investigation for the SAME
    # payment, fresh source_event_id -- exactly how workers/retry_scheduler.py
    # really re-invokes diagnose_and_persist on a fired reevaluation -- this
    # time recommending RETRY_NOW instead.
    monkeypatch.setattr(
        "services.diagnosis_engine.llm_client.gemini_generate_json",
        _one_round_response_recommending("RETRY_NOW"),
    )
    diagnosis_2 = await diagnose_and_persist(payment_id, source_event_id=str(uuid.uuid4()))
    assert diagnosis_2 is not None
    nba_2, decision_2, context_2 = await build_decision(
        payment_id, diagnosis_id=diagnosis_2.diagnosis_id
    )

    from recoveryos.config import get_settings

    get_settings.cache_clear()

    # Two distinct diagnoses, two distinct persisted recommendations, two
    # distinct final decisions for the SAME payment -- the causal claim the
    # audit asked for.
    assert diagnosis_2.diagnosis_id != diagnosis_1.diagnosis_id
    assert nba_2.chosen_action == "RETRY_NOW"
    assert nba_1.chosen_action != nba_2.chosen_action

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT diagnosis_id, recommended_action FROM recovery_recommendations "
                    "WHERE payment_id = :pid ORDER BY created_at ASC"
                ),
                {"pid": payment_id},
            )
        ).all()
    await engine.dispose()

    assert [r.recommended_action for r in rows] == ["ALT_ROUTE", "RETRY_NOW"]
