"""
Real AI Diagnoser — the LLM path.

TRD §9 threat model:
  - INPUT: exactly `DiagnosisInput`, dumped as JSON — never a raw DB row,
    never an f-string built from event payload text. CORRECTED (Domain
    Audit finding F3): this used to claim `failure_code` couldn't be a
    free-text injection vector because every field was "typed, bounded" --
    that was false; `failure_code` was unconstrained `str | None`. It's
    now sanitized (stripped to [A-Za-z0-9_], truncated to 64 chars) by
    `DiagnosisInput._sanitize_failure_code` (schemas.py) before this
    module ever sees it, and length/character-bounded at the API ingest
    boundary too (apps/api/routers/events.py's `pattern`) -- bounded, not
    absent.
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
from services.diagnosis_engine.guards import apply_adversarial_guards
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


async def _call_llm_openai(diagnosis_input: DiagnosisInput, model: str, api_key: str) -> dict:
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


# Gemini's responseSchema is a trimmed OpenAPI subset -- no
# `additionalProperties` keyword. Built from _RESPONSE_JSON_SCHEMA via the
# shared strip helper, not maintained by hand, so schema drift between
# providers isn't possible.
def _gemini_response_schema() -> dict:
    from services.diagnosis_engine.llm_client import strip_additional_properties

    return strip_additional_properties(_RESPONSE_JSON_SCHEMA)


async def _call_llm_gemini(diagnosis_input: DiagnosisInput, model: str, api_key: str) -> dict:
    from services.diagnosis_engine.llm_client import gemini_generate_json

    return await gemini_generate_json(
        system_prompt=_SYSTEM_PROMPT,
        user_content=_build_user_payload(diagnosis_input),
        response_schema=_gemini_response_schema(),
        model=model,
        api_key=api_key,
    )


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
    provider = settings.ai_diagnoser_provider
    if provider == "gemini":
        api_key = settings.gemini_api_key
        model = settings.ai_diagnoser_gemini_model
        call_fn = _call_llm_gemini
        missing_key_reason = "ai_diagnoser_not_configured_gemini"
        timeout_seconds = settings.ai_diagnoser_gemini_timeout_seconds
    elif provider == "openai":
        api_key = settings.openai_api_key
        model = settings.ai_diagnoser_model
        call_fn = _call_llm_openai
        missing_key_reason = "ai_diagnoser_not_configured_openai"
        timeout_seconds = settings.ai_diagnoser_timeout_seconds
    else:
        logger.warning(
            "[Diagnoser] Unknown ai_diagnoser_provider=%r -- skipping LLM path", provider
        )
        return None, "ai_diagnoser_unknown_provider"

    if not api_key:
        logger.info(
            "[Diagnoser] No API key configured for provider=%r -- skipping LLM path", provider
        )
        return None, missing_key_reason

    try:
        raw = await asyncio.wait_for(
            call_fn(diagnosis_input, model, api_key),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.warning("[Diagnoser] LLM call timed out after %.1fs", timeout_seconds)
        return None, "ai_diagnoser_timeout"
    except Exception as exc:  # network error, API error, anything the SDK raises
        logger.warning("[Diagnoser] LLM call failed: %s: %s", type(exc).__name__, exc)
        return None, f"ai_diagnoser_error_{type(exc).__name__}"

    try:
        evidence = [Evidence(**e) for e in raw["evidence"]]
        root_cause = RootCause(raw["root_cause"])
        confidence = float(raw["confidence"])
        # Task S2, pre-Phase-8 audit: PRD §37's adversarial guards (missing
        # bank -> confidence cap, conflicting bank signals -> override to
        # CONFLICTING_SIGNALS) used to run only for the fallback path,
        # despite guards.py's own docstring claiming uniform application.
        # The LLM is instructed via the system prompt to behave conservatively
        # in these cases, but that's a soft instruction, not a guarantee --
        # this makes it a hard one, identically for both paths.
        root_cause, confidence, evidence = apply_adversarial_guards(
            diagnosis_input, root_cause, confidence, evidence
        )
        output = DiagnosisOutput(
            root_cause=root_cause,
            confidence=confidence,
            evidence=evidence,
            cohort_id=None,
            model_version=f"{MODEL_VERSION_PREFIX}{provider}-{model}",
            is_fallback=False,
        )
        return output, ""
    except (ValidationError, KeyError, ValueError, TypeError) as exc:
        # Malformed/off-schema output — TRD §9: "output is schema-validated,
        # rejected if malformed." Treated exactly like a network failure:
        # return None, let the caller fall back. Never partially trusted.
        logger.warning("[Diagnoser] LLM response failed schema validation: %s", exc)
        return None, "ai_diagnoser_invalid_response"
