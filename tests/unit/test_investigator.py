"""
Task AGENT1 -- the investigative diagnosis loop (services/diagnosis_engine/
investigator.py). gemini_generate_json (the one low-level network call) and
call_tool (the one DB-touching call) are both mocked here -- this is a pure
loop-logic test, not a live-network test (see the separate live-proof script
for that, run manually against a real key on a handful of payments).
"""

from __future__ import annotations

import services.diagnosis_engine.investigator as investigator_module
from services.diagnosis_engine.investigator import (
    MAX_INVESTIGATION_ROUNDS,
    investigate,
)
from services.diagnosis_engine.schemas import DiagnosisInput, RecommendedAction, RootCause


def _base_input(**overrides) -> DiagnosisInput:
    defaults = {
        "payment_id": "pay_test",
        "amount_paise": 100000,
        "method": "upi",
        "bank": "HDFC",
        "failure_code": "TIMEOUT",
        "failure_class": None,
    }
    defaults.update(overrides)
    return DiagnosisInput(**defaults)


async def test_investigation_calls_a_tool_then_finalizes(monkeypatch):
    calls = {"round": []}

    async def fake_generate_json(*, system_prompt, user_content, response_schema, model, api_key):
        # Distinguish round-calls from the finalize-call by schema shape
        if "final_hypotheses" in user_content:
            return {
                "selected_cause": "temporary_bank_degradation",
                "confidence_band": "LIKELY",
                "evidence": [
                    {"fact": "cohort failure rate elevated", "source": "get_cohort_failure_rate"}
                ],
                "recommended_action": "RETRY_LATER",
                "recommended_delay_minutes": 30,
                "recommendation_confidence": 0.8,
                "risk_flags": [],
                "recovery_rationale": "cohort failure rate is elevated -- wait for it to recover",
            }
        calls["round"].append(user_content["round_number"])
        if user_content["round_number"] == 1:
            return {
                "hypotheses": [
                    {
                        "cause": "customer_specific",
                        "support_score": 3,
                        "contradict_score": 1,
                        "unresolved_questions": ["is this bank-wide?"],
                    },
                    {
                        "cause": "temporary_bank_degradation",
                        "support_score": 2,
                        "contradict_score": 2,
                        "unresolved_questions": [],
                    },
                ],
                "action": "call_tool",
                "tool_name": "get_cohort_failure_rate",
                "tool_inputs": {"bank": "HDFC", "method": "upi"},
                "expected_uncertainty_reduction": 6.0,
                "reasoning": "cohort data best distinguishes these two",
            }
        return {
            "hypotheses": [
                {
                    "cause": "temporary_bank_degradation",
                    "support_score": 7,
                    "contradict_score": 1,
                    "unresolved_questions": [],
                },
            ],
            "action": "finalize",
            "tool_name": "",
            "reasoning": "cohort data strongly favors systemic cause",
        }

    async def fake_call_tool(session, name, **kwargs):
        assert name == "get_cohort_failure_rate"
        return {"current_failure_rate": 0.4, "baseline_failure_rate": 0.05}

    monkeypatch.setattr(
        "services.diagnosis_engine.llm_client.gemini_generate_json", fake_generate_json
    )
    monkeypatch.setattr(investigator_module, "call_tool", fake_call_tool)

    result = await investigate(
        _base_input(),
        diagnoser_session=object(),
        model="gemini-2.5-flash-lite",
        api_key="fake",
        provider="gemini",
        round_timeout_seconds=5.0,
    )

    assert result is not None
    assert result.selected_cause == RootCause.TEMPORARY_BANK_DEGRADATION
    assert result.confidence_band == "LIKELY"
    assert result.confidence == 0.70
    assert len(result.steps) == 1
    assert result.steps[0].tool_name == "get_cohort_failure_rate"
    assert result.steps[0].expected_uncertainty_reduction == 6.0
    assert result.steps[0].investigation_score == 6.0 - 1.5 - (
        25 / 1000
    )  # cohort tool's real cost/latency
    assert len(result.hypotheses) >= 1
    assert calls["round"] == [1, 2]
    assert result.recommendation.recommended_action == RecommendedAction.RETRY_LATER
    assert result.recommendation.recommended_delay_minutes == 30
    assert result.recommendation.confidence == 0.70  # capped at the guard-applied diagnosis confidence (LIKELY)
    assert result.recommendation.risk_flags == []


async def test_non_gemini_provider_returns_none_immediately(monkeypatch):
    async def should_never_be_called(*args, **kwargs):
        raise AssertionError("must not call the network for a non-gemini provider")

    monkeypatch.setattr(
        "services.diagnosis_engine.llm_client.gemini_generate_json", should_never_be_called
    )

    result = await investigate(
        _base_input(),
        diagnoser_session=object(),
        model="gpt-4o-mini",
        api_key="fake",
        provider="openai",
        round_timeout_seconds=5.0,
    )
    assert result is None


async def test_round_timeout_falls_back_cleanly(monkeypatch):
    import asyncio

    async def hanging_generate_json(**kwargs):
        await asyncio.sleep(5.0)
        raise AssertionError("should never complete -- the timeout must fire first")

    monkeypatch.setattr(
        "services.diagnosis_engine.llm_client.gemini_generate_json", hanging_generate_json
    )

    result = await investigate(
        _base_input(),
        diagnoser_session=object(),
        model="gemini-2.5-flash-lite",
        api_key="fake",
        provider="gemini",
        round_timeout_seconds=0.05,
    )
    assert result is None


async def test_malformed_round_response_returns_none(monkeypatch):
    async def malformed_response(**kwargs):
        return {"hypotheses": []}  # missing required 'action' key

    monkeypatch.setattr(
        "services.diagnosis_engine.llm_client.gemini_generate_json", malformed_response
    )

    result = await investigate(
        _base_input(),
        diagnoser_session=object(),
        model="gemini-2.5-flash-lite",
        api_key="fake",
        provider="gemini",
        round_timeout_seconds=5.0,
    )
    assert result is None


async def test_invalid_tool_name_stops_investigation_without_crashing(monkeypatch):
    async def proposes_bogus_tool(*, system_prompt, user_content, response_schema, model, api_key):
        if "final_hypotheses" in user_content:
            return {
                "selected_cause": "unknown",
                "confidence_band": "INSUFFICIENT_EVIDENCE",
                "evidence": [{"fact": "investigation stopped early", "source": "system"}],
                "recommended_action": "ESCALATE",
                "recommended_delay_minutes": 0,
                "recommendation_confidence": 0.3,
                "risk_flags": [],
                "recovery_rationale": "investigation stopped early, insufficient evidence to recommend a retry",
            }
        return {
            "hypotheses": [
                {
                    "cause": "unknown",
                    "support_score": 0,
                    "contradict_score": 0,
                    "unresolved_questions": [],
                }
            ],
            "action": "call_tool",
            "tool_name": "drop_all_tables",  # not in TOOL_REGISTRY
            "tool_inputs": {},
            "expected_uncertainty_reduction": 5.0,
            "reasoning": "bogus",
        }

    monkeypatch.setattr(
        "services.diagnosis_engine.llm_client.gemini_generate_json", proposes_bogus_tool
    )

    result = await investigate(
        _base_input(),
        diagnoser_session=object(),
        model="gemini-2.5-flash-lite",
        api_key="fake",
        provider="gemini",
        round_timeout_seconds=5.0,
    )
    assert result is not None  # invalid tool choice stops the loop, doesn't crash it
    assert result.steps == []
    assert result.selected_cause == RootCause.UNKNOWN


async def test_adversarial_guard_applies_to_finalized_result(monkeypatch):
    """Missing bank metadata -> confidence must be capped, same guard as
    the single-call LLM path and the fallback path (Task S2)."""
    from services.diagnosis_engine.guards import MISSING_BANK_CONFIDENCE_CAP

    async def finalize_without_evidence_gathering(
        *, system_prompt, user_content, response_schema, model, api_key
    ):
        if "final_hypotheses" in user_content:
            return {
                "selected_cause": "temporary_bank_degradation",
                "confidence_band": "CONFIDENT",
                "evidence": [{"fact": "gateway timeout", "source": "payment_data"}],
                "recommended_action": "RETRY_NOW",
                "recommended_delay_minutes": 0,
                "recommendation_confidence": 0.9,
                "risk_flags": [],
                "recovery_rationale": "gateway timeout, likely transient",
            }
        return {
            "hypotheses": [
                {
                    "cause": "temporary_bank_degradation",
                    "support_score": 5,
                    "contradict_score": 0,
                    "unresolved_questions": [],
                }
            ],
            "action": "finalize",
            "reasoning": "clear enough already",
        }

    monkeypatch.setattr(
        "services.diagnosis_engine.llm_client.gemini_generate_json",
        finalize_without_evidence_gathering,
    )

    result = await investigate(
        _base_input(bank=None, failure_code="BANK_DOWN"),
        diagnoser_session=object(),
        model="gemini-2.5-flash-lite",
        api_key="fake",
        provider="gemini",
        round_timeout_seconds=5.0,
    )

    assert result is not None
    assert result.confidence <= MISSING_BANK_CONFIDENCE_CAP
    # recommendation.confidence is capped at the (guard-reduced) diagnosis
    # confidence too -- a downgraded diagnosis must not carry a falsely-high
    # recommendation confidence into the fusion tie-break math.
    assert result.recommendation.confidence <= MISSING_BANK_CONFIDENCE_CAP


def test_max_investigation_rounds_is_small():
    """Documents the deliberate choice -- each round is a real LLM call,
    kept small for free-tier quota conservation (explicit instruction)."""
    assert MAX_INVESTIGATION_ROUNDS <= 3
