"""
Real AI Diagnoser — the LLM path.

TRD §9 threat model:
  - INPUT: exactly `DiagnosisInput`, dumped as JSON — never a raw DB row,
    never an f-string built from event payload text. There is no
    free-text-concatenation step for a malicious `failure_code` string to
    exploit, because every field reaching the prompt is already a typed,
    bounded Pydantic field (str enum values, bounded floats, ints).
  - OUTPUT: OpenAI Structured Outputs (`response_format=json_schema,
    strict=True`) constrains the model to the exact shape below, then this
    module ALSO re-validates through the same `DiagnosisOutput` Pydantic
    model real fallback output uses — belt-and-suspenders, since "the API
    enforced the schema" and "our own code validated it" are two
    independent guarantees.

Failure handling: this function NEVER raises past its own boundary and
NEVER writes to any table. Timeout, network error, missing API key, or a
response that fails validation all collapse to the same outcome: return
None. The caller (diagnoser.py) is responsible for falling back — this
mirrors TRD §4.2's state machine exactly: DIAGNOSING --(AI timeout)-->
FALLBACK_DIAGNOSIS.
"""

from __future__ import annotations

import asyncio
import json
import logging

from pydantic import ValidationError

from recoveryos.config import get_settings
from services.diagnosis_engine.schemas import DiagnosisInput, DiagnosisOutput, Evidence, RootCause

logger = logging.getLogger(__name__)

MODEL_VERSION_PREFIX = "ai-diagnoser-"

_SYSTEM_PROMPT = (
    "You are a payment-failure root-cause diagnoser for a payments recovery system. "
    "You receive ONLY structured, pre-sanitized fields describing one failed payment "
    "and its current systemic anomaly context -- never raw customer data or free text. "
    "Respond with a JSON object matching the given schema exactly. "
    "root_cause must be one of: temporary_bank_degradation, systemic_degradation, "
    "permanent_failure, customer_specific, conflicting_signals, unknown. "
    "If the evidence is ambiguous, insufficient, or the signals actively contradict "
    "each other, respond with root_cause=unknown (or conflicting_signals if two "
    "specific signals disagree) and a LOW confidence -- never invent certainty. "
    "Every evidence entry must cite a SPECIFIC structured fact you were given, e.g. "
    "'bank=HDFC observed_rate=0.148 baseline=0.031 z=7.1' -- not vague reasoning."
)

_RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string", "enum": [rc.value for rc in RootCause]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "evidence": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["fact", "source"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["root_cause", "confidence", "evidence"],
    "additionalProperties": False,
}


def _build_user_payload(diagnosis_input: DiagnosisInput) -> dict:
    """The entire prompt content. Dumping the validated Pydantic model
    directly — not string-formatting it into prose — is what makes this a
    real TRD §9 boundary rather than a convention: there's no step where a
    field's raw string value gets spliced into free text."""
    return diagnosis_input.model_dump(mode="json")


async def _call_llm(diagnosis_input: DiagnosisInput, model: str, api_key: str) -> dict:
    # Imported lazily so importing this module (e.g. for the fallback-only
    # test suite) never requires the `openai` package to even be installed,
    # let alone configured.
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key)
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(_build_user_payload(diagnosis_input))},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "diagnosis", "schema": _RESPONSE_JSON_SCHEMA, "strict": True},
        },
    )
    content = response.choices[0].message.content
    return json.loads(content)


async def diagnose_with_llm(diagnosis_input: DiagnosisInput) -> tuple[DiagnosisOutput | None, str]:
    """
    Attempt a real AI diagnosis. Returns (output, reason) — on success,
    output is non-None and reason is "". On ANY failure, output is None and
    reason is a short, specific machine string identifying WHY: timeout,
    network error, missing API key, or a response that failed schema
    validation — this is what gaps.md §A.3's fallback evidence
    ("fallback_triggered=true, reason=...") reports, so the audit explorer
    shows *why* the fallback fired, not just that it did. The caller falls
    back on any non-empty reason; this function never raises.

    cohort_id is always None here — see fallback_rules.py's module
    docstring: cohort attachment happens once, uniformly, in diagnoser.py's
    orchestrator, for whichever path (this one or the fallback) produced
    the diagnosis.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        logger.info("[Diagnoser] No OPENAI_API_KEY configured -- skipping LLM path")
        return None, "ai_diagnoser_not_configured"

    try:
        raw = await asyncio.wait_for(
            _call_llm(diagnosis_input, settings.ai_diagnoser_model, settings.openai_api_key),
            timeout=settings.ai_diagnoser_timeout_seconds,
        )
    except TimeoutError:
        logger.warning(
            "[Diagnoser] LLM call timed out after %.1fs", settings.ai_diagnoser_timeout_seconds
        )
        return None, "ai_diagnoser_timeout"
    except Exception as exc:  # network error, API error, anything the SDK raises
        logger.warning("[Diagnoser] LLM call failed: %s: %s", type(exc).__name__, exc)
        return None, f"ai_diagnoser_error_{type(exc).__name__}"

    try:
        evidence = [Evidence(**e) for e in raw["evidence"]]
        output = DiagnosisOutput(
            root_cause=RootCause(raw["root_cause"]),
            confidence=float(raw["confidence"]),
            evidence=evidence,
            cohort_id=None,
            model_version=f"{MODEL_VERSION_PREFIX}{settings.ai_diagnoser_model}",
            is_fallback=False,
        )
        return output, ""
    except (ValidationError, KeyError, ValueError, TypeError) as exc:
        # Malformed/off-schema output — TRD §9: "output is schema-validated,
        # rejected if malformed." Treated exactly like a network failure:
        # return None, let the caller fall back. Never partially trusted.
        logger.warning("[Diagnoser] LLM response failed schema validation: %s", exc)
        return None, "ai_diagnoser_invalid_response"
