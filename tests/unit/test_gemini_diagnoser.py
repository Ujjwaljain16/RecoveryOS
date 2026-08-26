"""
Priority 11 -- Gemini provider for the real LLM diagnosis path.

Mirrors test_llm_diagnoser_guards.py's OpenAI-path coverage exactly, but
through _call_llm_gemini, proving the SAME TRD §9 boundary (typed input,
schema-constrained output, Pydantic re-validation, apply_adversarial_guards)
holds regardless of which provider ai_diagnoser_provider selects -- provider
choice must never be able to weaken the safety boundary.
"""

from __future__ import annotations

import httpx
import pytest

import services.diagnosis_engine.llm_diagnoser as llm_diagnoser_module
from services.diagnosis_engine.guards import (
    CONFLICTING_SIGNALS_CONFIDENCE_CAP,
    MISSING_BANK_CONFIDENCE_CAP,
)
from services.diagnosis_engine.llm_diagnoser import (
    _gemini_response_schema,
    diagnose_with_llm,
)
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


def test_gemini_response_schema_has_no_additional_properties_keyword():
    """Gemini's schema dialect doesn't accept additionalProperties -- the
    schema sent to Gemini must be the OpenAI schema with that keyword
    stripped at every nesting level, not a hand-maintained duplicate that
    could silently drift from the real one."""

    def _walk(node):
        if isinstance(node, dict):
            assert "additionalProperties" not in node
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    schema = _gemini_response_schema()
    _walk(schema)
    # still constrains the same fields -- stripping additionalProperties
    # must not have silently dropped anything else.
    assert schema["required"] == ["root_cause", "confidence", "evidence"]
    assert schema["properties"]["root_cause"]["enum"] == [rc.value for rc in RootCause]


async def _diagnose_with_mocked_gemini(monkeypatch, diagnosis_input, root_cause, confidence):
    monkeypatch.setenv("AI_DIAGNOSER_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-real-key")
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    async def _fake_call_llm_gemini(_diagnosis_input, _model, _api_key):
        return {
            "root_cause": root_cause,
            "confidence": confidence,
            "evidence": [{"fact": "mocked gemini evidence", "source": "system"}],
        }

    monkeypatch.setattr(llm_diagnoser_module, "_call_llm_gemini", _fake_call_llm_gemini)

    output, reason = await diagnose_with_llm(diagnosis_input)
    get_settings.cache_clear()
    return output, reason


@pytest.mark.asyncio
async def test_gemini_path_missing_bank_metadata_lowers_confidence(monkeypatch):
    diagnosis_input = _base_input(bank=None, failure_code="BANK_DOWN")
    output, reason = await _diagnose_with_mocked_gemini(
        monkeypatch, diagnosis_input, "temporary_bank_degradation", confidence=0.95
    )
    assert output is not None and reason == ""
    assert output.confidence <= MISSING_BANK_CONFIDENCE_CAP
    assert not output.is_fallback


@pytest.mark.asyncio
async def test_gemini_path_conflicting_signals_flagged_not_silently_resolved(monkeypatch):
    """failure_code=BANK_DOWN but the anomaly detector reports this bank as
    healthy right now -- mirrors test_llm_path_conflicting_signals_flagged_
    not_silently_resolved's exact fixture."""
    diagnosis_input = _base_input(
        bank="HDFC",
        failure_code="BANK_DOWN",
        is_anomaly=False,
        anomaly_scope_type="bank",
        anomaly_scope_entity="HDFC",
        anomaly_severity="low",
    )
    output, reason = await _diagnose_with_mocked_gemini(
        monkeypatch, diagnosis_input, "temporary_bank_degradation", confidence=0.85
    )
    assert output is not None and reason == ""
    assert output.root_cause == RootCause.CONFLICTING_SIGNALS
    assert output.confidence <= CONFLICTING_SIGNALS_CONFIDENCE_CAP


@pytest.mark.asyncio
async def test_gemini_path_non_adversarial_input_is_unaffected(monkeypatch):
    diagnosis_input = _base_input(bank="HDFC", failure_code="TIMEOUT")
    output, reason = await _diagnose_with_mocked_gemini(
        monkeypatch, diagnosis_input, "customer_specific", confidence=0.8
    )
    assert output is not None and reason == ""
    assert output.confidence == 0.8
    assert output.root_cause == RootCause.CUSTOMER_SPECIFIC


@pytest.mark.asyncio
async def test_gemini_missing_api_key_falls_back_cleanly(monkeypatch):
    """No GEMINI_API_KEY configured -> same fail-closed contract as the
    OpenAI path: return (None, reason), never raise, caller falls back."""
    monkeypatch.setenv("AI_DIAGNOSER_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    output, reason = await diagnose_with_llm(_base_input())
    get_settings.cache_clear()

    assert output is None
    assert reason == "ai_diagnoser_not_configured_gemini"


@pytest.mark.asyncio
async def test_gemini_http_error_falls_back_cleanly(monkeypatch):
    """A real HTTP-level failure (rate limit, 500, etc.) from Gemini must
    collapse to the same (None, reason) contract, never raise past the
    diagnose_with_llm() boundary -- proven against httpx's own exception
    type, not a generic stand-in."""
    monkeypatch.setenv("AI_DIAGNOSER_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-real-key")
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    async def _rate_limited_call_llm_gemini(_diagnosis_input, _model, _api_key):
        request = httpx.Request("POST", "https://generativelanguage.googleapis.com/x")
        response = httpx.Response(429, request=request, text="rate limited")
        raise httpx.HTTPStatusError("429 rate limited", request=request, response=response)

    monkeypatch.setattr(llm_diagnoser_module, "_call_llm_gemini", _rate_limited_call_llm_gemini)

    output, reason = await diagnose_with_llm(_base_input())
    get_settings.cache_clear()

    assert output is None
    assert reason.startswith("ai_diagnoser_error_")
