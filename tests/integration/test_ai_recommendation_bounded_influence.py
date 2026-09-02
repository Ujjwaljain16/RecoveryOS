"""
Phase 11 -- the POSITIVE and NEGATIVE proof of bounded AI-recommendation
fusion, the counterpart to tests/integration/test_diagnosis_has_no_decision_authority.py's
"stays AI-blind by default" invariant. See services/recovery_engine/orchestrator.py's
_apply_ai_fusion docstring for the exact boundary being proven here.

services/recovery_engine/orchestrator.generate_candidate_actions is
monkeypatched to a fixed, hand-picked set of 6 CandidateActionResult values
per test -- this isolates the fusion logic itself from the certified
propensity model / action_costs table, exactly like tests/unit/test_investigator.py
mocks the LLM boundary while leaving everything else (real DB, real
policy_engine.evaluate(), real persistence) genuinely real.
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


def _pg_text_array(values: list[str]) -> str:
    if not values:
        return "'{}'::text[]"
    return "ARRAY[" + ",".join(repr(v) for v in values) + "]::text[]"


async def _insert_payment(
    migrated_db: str, *, amount_paise: int = 200_000, method: str = "card"
) -> tuple[str, str, str]:
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
    return payment_id, merchant_id, customer_id


async def _insert_diagnosis_and_recommendation(
    migrated_db: str,
    *,
    payment_id: str,
    recommended_action: str,
    confidence: float,
    risk_flags: list[str] | None = None,
) -> str:
    """Returns diagnosis_id. Both rows inserted directly by SQL -- this is a
    fusion-logic test, not an investigator/LLM test, so no real diagnose()
    call is needed."""
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
            {"did": diagnosis_id, "pid": payment_id, "action": recommended_action, "conf": confidence},
        )
    await engine.dispose()
    return diagnosis_id


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


def _enable_fusion(monkeypatch, *, enabled: bool = True, tolerance_bps: int = 100):
    monkeypatch.setenv("AI_RECOMMENDATION_FUSION_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("AI_TIE_BREAK_TOLERANCE_BPS", str(tolerance_bps))
    from recoveryos.config import get_settings

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_tie_break_applies_for_near_tied_policy_allowed_recommendation(
    migrated_db, monkeypatch
):
    """The user's own worked example: RETRY_NOW ₹82.00 vs ALT_ROUTE ₹81.70 --
    AI recommends ALT_ROUTE, it's within 1% tolerance and individually
    policy-ALLOWED, so it wins."""
    payment_id, _, _ = await _insert_payment(migrated_db)
    diagnosis_id = await _insert_diagnosis_and_recommendation(
        migrated_db, payment_id=payment_id, recommended_action="ALT_ROUTE", confidence=0.9
    )
    monkeypatch.setattr(
        orchestrator_module,
        "generate_candidate_actions",
        _fixed_candidates({"RETRY_NOW": 8_200, "ALT_ROUTE": 8_170, "REMINDER": 1_000, "ESCALATE": 500}),
    )
    _enable_fusion(monkeypatch)

    nba_result, decision, context = await build_decision(payment_id, diagnosis_id=diagnosis_id)

    assert nba_result.chosen_action == "ALT_ROUTE"
    assert decision.verdict == "ALLOW"
    assert context["fusion_provenance"]["tie_break_applied"] is True
    assert context["fusion_provenance"]["final_action"] == "ALT_ROUTE"


@pytest.mark.asyncio
async def test_tie_break_does_not_apply_for_decisive_winner(migrated_db, monkeypatch):
    """The user's own second worked example: RETRY_NOW ₹82 vs ALT_ROUTE ₹64 --
    a decisive ~22% delta. AI recommends ALT_ROUTE anyway; it must be
    ignored."""
    payment_id, _, _ = await _insert_payment(migrated_db)
    diagnosis_id = await _insert_diagnosis_and_recommendation(
        migrated_db, payment_id=payment_id, recommended_action="ALT_ROUTE", confidence=0.99
    )
    monkeypatch.setattr(
        orchestrator_module,
        "generate_candidate_actions",
        _fixed_candidates({"RETRY_NOW": 8_200, "ALT_ROUTE": 6_400}),
    )
    _enable_fusion(monkeypatch)

    nba_result, decision, context = await build_decision(payment_id, diagnosis_id=diagnosis_id)

    assert nba_result.chosen_action == "RETRY_NOW"
    assert context["fusion_provenance"]["tie_break_applied"] is False
    assert context["fusion_provenance"]["reject_reason"] == "outside_tolerance"


@pytest.mark.asyncio
async def test_tie_break_rejected_when_near_tied_candidate_is_individually_policy_blocked(
    migrated_db, monkeypatch
):
    """AI recommends RETRY_NOW, economically near-tied with the ALT_ROUTE
    winner -- but this payment is a UPI e-mandate above the RBI AFA-exempt
    threshold, so RETRY_NOW individually fails EMandateRetryComplianceRule
    on its own re-evaluation. The deterministic winner (ALT_ROUTE, which
    that rule doesn't apply to) must stand -- proving AI can never select a
    candidate policy has rejected, even an economically near-tied one."""
    payment_id, _, _ = await _insert_payment(migrated_db, amount_paise=1_600_000, method="upi")
    diagnosis_id = await _insert_diagnosis_and_recommendation(
        migrated_db, payment_id=payment_id, recommended_action="RETRY_NOW", confidence=0.9
    )
    monkeypatch.setattr(
        orchestrator_module,
        "generate_candidate_actions",
        _fixed_candidates({"ALT_ROUTE": 8_200, "RETRY_NOW": 8_170, "REMINDER": 1_000}),
    )
    _enable_fusion(monkeypatch)

    nba_result, decision, context = await build_decision(payment_id, diagnosis_id=diagnosis_id)

    assert nba_result.chosen_action == "ALT_ROUTE"
    assert decision.verdict == "ALLOW"
    assert context["fusion_provenance"]["tie_break_applied"] is False
    assert context["fusion_provenance"]["reject_reason"] == "tie_break_rejected_policy"


@pytest.mark.asyncio
async def test_risk_flag_escalates_regardless_of_strongly_positive_evi(migrated_db, monkeypatch):
    """Invariant 4 (Phase 11 design doc): a risk flag forces ESCALATE even
    when the deterministic winner's own EVI is strongly positive -- the
    flag can only ever route to the safety rule, never authorize the
    money-moving action it was attached to."""
    payment_id, _, _ = await _insert_payment(migrated_db)
    diagnosis_id = await _insert_diagnosis_and_recommendation(
        migrated_db,
        payment_id=payment_id,
        recommended_action="RETRY_NOW",
        confidence=0.95,
        risk_flags=["HIGH_FRAUD_RISK"],
    )
    monkeypatch.setattr(
        orchestrator_module, "generate_candidate_actions", _fixed_candidates({"RETRY_NOW": 1_000_000})
    )
    _enable_fusion(monkeypatch)

    nba_result, decision, context = await build_decision(payment_id, diagnosis_id=diagnosis_id)

    assert decision.verdict == "ESCALATE"
    assert context["fusion_provenance"]["risk_escalation_applied"] is True
    assert decision.rule_trace[-1]["rule"] == "AIRiskSignalEscalationRule"


@pytest.mark.asyncio
async def test_fusion_disabled_ignores_recommendation_even_though_it_exists(migrated_db, monkeypatch):
    payment_id, _, _ = await _insert_payment(migrated_db)
    diagnosis_id = await _insert_diagnosis_and_recommendation(
        migrated_db, payment_id=payment_id, recommended_action="ALT_ROUTE", confidence=0.9
    )
    monkeypatch.setattr(
        orchestrator_module,
        "generate_candidate_actions",
        _fixed_candidates({"RETRY_NOW": 8_200, "ALT_ROUTE": 8_170}),
    )
    _enable_fusion(monkeypatch, enabled=False)

    nba_result, decision, context = await build_decision(payment_id, diagnosis_id=diagnosis_id)

    assert nba_result.chosen_action == "RETRY_NOW"
    assert context["fusion_provenance"] is None


@pytest.mark.asyncio
async def test_omitted_diagnosis_id_ignores_recommendation_even_when_fusion_enabled(
    migrated_db, monkeypatch
):
    payment_id, _, _ = await _insert_payment(migrated_db)
    await _insert_diagnosis_and_recommendation(
        migrated_db, payment_id=payment_id, recommended_action="ALT_ROUTE", confidence=0.9
    )
    monkeypatch.setattr(
        orchestrator_module,
        "generate_candidate_actions",
        _fixed_candidates({"RETRY_NOW": 8_200, "ALT_ROUTE": 8_170}),
    )
    _enable_fusion(monkeypatch, enabled=True)

    nba_result, decision, context = await build_decision(payment_id)  # diagnosis_id omitted

    assert nba_result.chosen_action == "RETRY_NOW"
    assert context["fusion_provenance"]["ai_recommended_action"] is None
    assert context["fusion_provenance"]["fusion_reason"] == "no_recommendation_available"
