"""
Task S2 (pre-Phase-8 audit): apply_adversarial_guards() must fire identically
for the real LLM path, not just the deterministic fallback. guards.py's own
module docstring always claimed uniform application; before this fix,
diagnose_with_llm() never actually called it -- test_diagnosis_adversarial.py
only ever exercised diagnose_fallback(). These tests reuse the exact same
scenario fixtures against the real diagnose_with_llm() path, with only the
OpenAI SDK call itself mocked (via _call_llm, the same seam
test_diagnoser_timeout_falls_back_to_deterministic_rule already uses).
"""

from __future__ import annotations

import services.diagnosis_engine.llm_diagnoser as llm_diagnoser_module
from services.diagnosis_engine.guards import (
    CONFLICTING_SIGNALS_CONFIDENCE_CAP,
    MISSING_BANK_CONFIDENCE_CAP,
)
from services.diagnosis_engine.llm_diagnoser import diagnose_with_llm
from services.diagnosis_engine.schemas import DiagnosisInput, RootCause


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


async def _mock_llm_raw_response(root_cause: str, confidence: float) -> dict:
    return {
        "root_cause": root_cause,
        "confidence": confidence,
        "evidence": [{"fact": "mocked LLM evidence", "source": "system"}],
    }


async def _diagnose_with_mocked_llm(monkeypatch, diagnosis_input, root_cause, confidence):
    # Pinned to 'openai' regardless of the ambient .env's AI_DIAGNOSER_PROVIDER
    # (defaults to 'gemini' for local/docker runs) -- this test exercises the
    # provider-agnostic guard logic via whichever call_fn diagnose_with_llm()
    # dispatches to, and must not silently stop mocking the right seam.
    monkeypatch.setenv("AI_DIAGNOSER_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    async def _fake_call_llm(_diagnosis_input, _model, _api_key):
        return await _mock_llm_raw_response(root_cause, confidence)

    monkeypatch.setattr(llm_diagnoser_module, "_call_llm_openai", _fake_call_llm)

    output, reason = await diagnose_with_llm(diagnosis_input)
    get_settings.cache_clear()
    return output, reason


async def test_llm_path_missing_bank_metadata_lowers_confidence(monkeypatch):
    """Mirrors test_diagnosis_adversarial.py's fallback-path test exactly,
    against the real LLM path: the LLM confidently returns
    temporary_bank_degradation at 0.9 with bank=None -- the guard must still
    cap it, exactly as it does for the fallback."""
    output, reason = await _diagnose_with_mocked_llm(
        monkeypatch,
        _base_input(bank=None),
        root_cause="temporary_bank_degradation",
        confidence=0.9,
    )

    assert reason == ""
    assert output is not None
    assert output.is_fallback is False, "this must be the real LLM path, not a fallback"
    assert output.root_cause == RootCause.TEMPORARY_BANK_DEGRADATION
    assert output.confidence <= MISSING_BANK_CONFIDENCE_CAP, (
        f"LLM path must be guarded identically to the fallback path -- got confidence="
        f"{output.confidence}, expected <= {MISSING_BANK_CONFIDENCE_CAP}"
    )
    assert any("bank_metadata_missing" in e.fact for e in output.evidence)


async def test_llm_path_conflicting_signals_flagged_not_silently_resolved(monkeypatch):
    """Mirrors test_diagnosis_adversarial.py's conflicting-signals test
    against the real LLM path: failure_code=BANK_DOWN but the anomaly
    detector reports this bank as healthy right now. The LLM confidently
    returns temporary_bank_degradation at 0.85 -- the guard must override to
    CONFLICTING_SIGNALS, exactly as it does for the fallback."""
    conflicting = _base_input(
        bank="HDFC",
        failure_code="BANK_DOWN",
        is_anomaly=False,
        anomaly_scope_type="bank",
        anomaly_scope_entity="HDFC",
        anomaly_severity="low",
    )
    output, reason = await _diagnose_with_mocked_llm(
        monkeypatch, conflicting, root_cause="temporary_bank_degradation", confidence=0.85
    )

    assert reason == ""
    assert output is not None
    assert output.is_fallback is False
    assert output.root_cause == RootCause.CONFLICTING_SIGNALS, (
        "LLM path must be overridden to CONFLICTING_SIGNALS identically to the fallback path, "
        f"got {output.root_cause}"
    )
    assert output.confidence <= CONFLICTING_SIGNALS_CONFIDENCE_CAP
    assert any(
        "conflict:" in e.fact for e in output.evidence
    ), "the conflict itself must be recorded as evidence for the LLM path too"


async def test_llm_path_non_adversarial_input_is_unaffected(monkeypatch):
    """Sanity: a clean input (bank present, no conflict) must pass through
    the guards unchanged -- confirms the guard call doesn't clip every LLM
    response indiscriminately."""
    output, reason = await _diagnose_with_mocked_llm(
        monkeypatch,
        _base_input(bank="HDFC", failure_code="TIMEOUT"),
        root_cause="temporary_bank_degradation",
        confidence=0.82,
    )

    assert reason == ""
    assert output is not None
    assert output.root_cause == RootCause.TEMPORARY_BANK_DEGRADATION
    assert output.confidence == 0.82, "a non-adversarial input must not have its confidence altered"
