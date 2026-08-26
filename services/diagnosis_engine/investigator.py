"""
Investigative diagnoser -- Task AGENT1. Replaces a single one-shot LLM
classification call with a bounded, tool-calling investigation loop:

    hypothesize -> (select tool by InvestigationScore -> call tool -> update
    hypotheses) x up to MAX_INVESTIGATION_ROUNDS -> finalize

Same TRD §9 boundary as llm_diagnoser.py's single-call path, extended, not
replaced: typed input only (DiagnosisInput dumped as JSON, same as before),
every LLM response schema-validated before use, read-only tools running on
the diagnoser_role connection (services/diagnosis_engine/tools.py) with
ZERO write access, and this function NEVER raises past its own boundary --
any failure at any point returns None and the caller (diagnoser.py) falls
back to the existing deterministic diagnose_fallback(), unchanged.

Design choices, per the agent-design review:
  Point 1 -- hypotheses carry support_score/contradict_score/evidence_count,
    NOT a probability. Nothing here claims to be a calibrated statistical
    model. confidence_band (CONFIDENT|LIKELY|AMBIGUOUS|
    INSUFFICIENT_EVIDENCE|CONFLICTING_SIGNALS|ESCALATE) is the only
    confidence signal the finalize step produces, and it maps to the
    existing numeric `confidence` column only as a fixed representative
    value for backward compatibility with EVI/policy consumers that
    already read a float -- that mapping is disclosed, not hidden.
  Point 2 -- tools are drawn from TOOL_REGISTRY only; the investigator
    can never invent a tool name or query shape.
  Point 3 -- InvestigationScore = expected_uncertainty_reduction (an
    LLM-ESTIMATED score, explicitly labeled as such everywhere it's
    used/persisted) - tool_cost - latency_penalty, where tool_cost and
    latency come from the registry's own real, fixed constants.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from services.diagnosis_engine.guards import apply_adversarial_guards
from services.diagnosis_engine.schemas import DiagnosisInput, Evidence, RootCause
from services.diagnosis_engine.tools import TOOL_REGISTRY, call_tool

logger = logging.getLogger(__name__)

MAX_INVESTIGATION_ROUNDS = 2  # kept small deliberately -- each round is a real LLM call

# confidence_band -> a fixed representative float, for the existing
# DiagnosisOutput.confidence column (EVI/policy read a float today). This
# is a disclosed, fixed mapping, not a claim that the band IS this number.
CONFIDENCE_BAND_TO_FLOAT = {
    "CONFIDENT": 0.90,
    "LIKELY": 0.70,
    "AMBIGUOUS": 0.50,
    "INSUFFICIENT_EVIDENCE": 0.30,
    "CONFLICTING_SIGNALS": 0.35,
    "ESCALATE": 0.20,
}

_ROUND_SCHEMA = {
    "type": "object",
    "properties": {
        "hypotheses": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "cause": {"type": "string", "enum": [rc.value for rc in RootCause]},
                    "support_score": {"type": "integer", "minimum": 0, "maximum": 10},
                    "contradict_score": {"type": "integer", "minimum": 0, "maximum": 10},
                    "unresolved_questions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["cause", "support_score", "contradict_score", "unresolved_questions"],
            },
        },
        "action": {"type": "string", "enum": ["call_tool", "finalize"]},
        # Not in `required` -- genuinely optional (omitted/null when
        # action=finalize), not an empty-string enum member. Gemini's
        # schema validator rejects an empty string inside an `enum` list
        # outright ("cannot be empty"), so a real "no tool" sentinel had to
        # be represented by absence, not a fake extra enum value.
        "tool_name": {"type": "string", "enum": list(TOOL_REGISTRY.keys())},
        # No tool_inputs field: every tool's arguments are server-derived
        # from diagnosis_input (_derive_tool_inputs), not supplied by the
        # model -- the investigator only ever chooses WHICH tool runs.
        "expected_uncertainty_reduction": {"type": "number", "minimum": 0.0, "maximum": 10.0},
        "reasoning": {"type": "string"},
    },
    "required": ["hypotheses", "action", "reasoning"],
}

_FINALIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "selected_cause": {"type": "string", "enum": [rc.value for rc in RootCause]},
        "confidence_band": {
            "type": "string",
            "enum": list(CONFIDENCE_BAND_TO_FLOAT.keys()),
        },
        "evidence": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {"fact": {"type": "string"}, "source": {"type": "string"}},
                "required": ["fact", "source"],
            },
        },
    },
    "required": ["selected_cause", "confidence_band", "evidence"],
}

_ROUND_SYSTEM_PROMPT = (
    "You are a payment-failure investigation planner for a payments recovery system. "
    "You maintain a list of competing root-cause hypotheses and decide, each round, "
    "whether to gather more evidence or finalize. "
    "support_score/contradict_score are small integer counts of how much evidence backs "
    "or contradicts each hypothesis -- NOT a probability, and must never be reported as one. "
    "If you choose action=call_tool, name exactly one tool from the provided registry "
    "(its arguments are supplied automatically -- you only choose which tool runs) and "
    "estimate expected_uncertainty_reduction (0-10): "
    "how much this specific tool call would help distinguish between your current "
    "hypotheses, if it truly is your best estimate -- an honest low score is fine and "
    "expected when no remaining tool would help much. "
    "If evidence already clearly favors one hypothesis, or no available tool would "
    "meaningfully help, choose action=finalize."
)

_FINALIZE_SYSTEM_PROMPT = (
    "You are finalizing a payment-failure root-cause investigation. Given the final "
    "hypotheses and the evidence gathered, select the single best-supported root cause "
    "and an honest confidence_band. Use CONFLICTING_SIGNALS when two hypotheses have "
    "comparable strong support that contradicts each other, and INSUFFICIENT_EVIDENCE or "
    "ESCALATE when nothing clearly stands out -- never invent certainty. Every evidence "
    "entry must cite a SPECIFIC fact gathered during the investigation, not vague reasoning."
)


@dataclass(frozen=True)
class Hypothesis:
    cause: str
    support_score: int
    contradict_score: int
    evidence_count: int
    unresolved_questions: list[str]


@dataclass(frozen=True)
class InvestigationStep:
    step_number: int
    tool_name: str
    tool_inputs: dict
    tool_output_summary: dict | list | None
    expected_uncertainty_reduction: float
    tool_cost: float
    latency_ms: int | None
    investigation_score: float


@dataclass(frozen=True)
class InvestigationResult:
    hypotheses: list[Hypothesis]
    selected_cause: RootCause
    confidence_band: str
    confidence: float  # CONFIDENCE_BAND_TO_FLOAT[confidence_band] -- disclosed mapping
    evidence: list[Evidence]
    steps: list[InvestigationStep] = field(default_factory=list)


def _derive_tool_inputs(tool_name: str, diagnosis_input: DiagnosisInput) -> dict:
    """
    Every tool in TOOL_REGISTRY takes either payment_id or (bank, method) --
    both already known from diagnosis_input. Server-derives them rather
    than trusting the LLM to echo them back correctly: a live test showed
    the model choosing the right tool but omitting/mis-naming required
    arguments, which is exactly the class of error this sidesteps
    entirely. The investigator chooses WHICH tool runs; it never controls
    what arguments that tool actually receives.
    """
    if tool_name in ("get_customer_payment_history", "get_customer_recovery_history",
                     "get_payment_attempt_history", "get_intervention_history"):
        return {"payment_id": diagnosis_input.payment_id}
    if tool_name in ("get_cohort_failure_rate", "get_recent_anomalies"):
        return {"bank": diagnosis_input.bank, "method": diagnosis_input.method}
    return {}


def _json_safe(value):
    """tools.py's query results carry native asyncpg/SQLAlchemy types
    (uuid.UUID, datetime, Decimal) straight out of .mappings() -- none of
    those are JSON-serializable, and a live test hit exactly this: a real
    tool call succeeded, then the next round's prompt-building crashed on
    json.dumps(). Recursively stringifies anything json.dumps can't handle
    natively."""
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _summarize_tool_output(output) -> dict | list:
    """Truncate a tool's raw result before it re-enters the next round's
    prompt -- keeps the payload small and avoids re-feeding an entire
    history table back to the model verbatim. Also makes it JSON-safe
    (see _json_safe) since it's about to be embedded in the next round's
    prompt payload."""
    truncated = output[:5] if isinstance(output, list) else output
    return _json_safe(truncated)


async def investigate(
    diagnosis_input: DiagnosisInput,
    diagnoser_session: AsyncSession,
    *,
    model: str,
    api_key: str,
    provider: str,
    round_timeout_seconds: float,
) -> InvestigationResult | None:
    """
    Runs the full loop. Returns None on ANY failure (timeout, network error,
    schema validation failure, unknown tool name, or a provider other than
    'gemini' -- see _generic_llm_json_call's docstring for why only Gemini
    is wired here today) -- the caller falls back to diagnose_fallback(),
    same fail-closed contract as the single-call path.
    """
    if provider != "gemini":
        logger.info("[Investigator] provider=%r has no multi-round loop wired -- skipping", provider)
        return None

    try:
        return await _run_investigation(
            diagnosis_input, diagnoser_session, model, api_key, round_timeout_seconds
        )
    except TimeoutError:
        logger.warning("[Investigator] round timed out after %.1fs", round_timeout_seconds)
        return None
    except (ValidationError, KeyError, ValueError, TypeError) as exc:
        logger.warning("[Investigator] schema/validation failure: %s", exc)
        return None
    except Exception as exc:  # network error, API error, anything the SDK raises
        logger.warning("[Investigator] failed: %s: %s", type(exc).__name__, exc)
        return None


async def _run_investigation(
    diagnosis_input: DiagnosisInput,
    diagnoser_session: AsyncSession,
    model: str,
    api_key: str,
    round_timeout_seconds: float,
) -> InvestigationResult:
    payload = diagnosis_input.model_dump(mode="json")
    hypotheses: list[Hypothesis] = []
    steps: list[InvestigationStep] = []
    evidence_log: list[dict] = []
    used_tools: set[str] = set()

    for round_number in range(1, MAX_INVESTIGATION_ROUNDS + 1):
        available_tools = {
            name: {"purpose": spec.purpose, "tool_cost": spec.tool_cost,
                   "latency_ms_estimate": spec.latency_ms_estimate}
            for name, spec in TOOL_REGISTRY.items()
            if name not in used_tools
        }
        round_input = {
            "payment": payload,
            "round_number": round_number,
            "max_rounds": MAX_INVESTIGATION_ROUNDS,
            "current_hypotheses": [
                {"cause": h.cause, "support_score": h.support_score,
                 "contradict_score": h.contradict_score,
                 "unresolved_questions": h.unresolved_questions}
                for h in hypotheses
            ],
            "evidence_gathered_so_far": evidence_log,
            "available_tools": available_tools,
        }
        raw = await asyncio.wait_for(
            _generic_llm_json_call(
                _ROUND_SYSTEM_PROMPT, round_input, _ROUND_SCHEMA, model, api_key
            ),
            timeout=round_timeout_seconds,
        )

        hypotheses = [
            Hypothesis(
                cause=h["cause"],
                support_score=int(h["support_score"]),
                contradict_score=int(h["contradict_score"]),
                evidence_count=len(evidence_log),
                unresolved_questions=list(h["unresolved_questions"]),
            )
            for h in raw["hypotheses"]
        ]

        if raw["action"] == "finalize" or not raw.get("tool_name"):
            break

        tool_name = raw["tool_name"]
        if tool_name not in TOOL_REGISTRY or tool_name in used_tools:
            break  # investigator proposed something invalid/repeated -- stop, don't loop forever
        spec = TOOL_REGISTRY[tool_name]
        expected_gain = float(raw.get("expected_uncertainty_reduction") or 0.0)
        latency_penalty = spec.latency_ms_estimate / 1000.0
        investigation_score = expected_gain - spec.tool_cost - latency_penalty

        tool_inputs = _derive_tool_inputs(tool_name, diagnosis_input)
        t0 = time.monotonic()
        tool_output = await call_tool(diagnoser_session, tool_name, **tool_inputs)
        latency_ms = int((time.monotonic() - t0) * 1000)

        summary = _summarize_tool_output(tool_output)
        steps.append(
            InvestigationStep(
                step_number=round_number,
                tool_name=tool_name,
                tool_inputs=tool_inputs,
                tool_output_summary=summary,
                expected_uncertainty_reduction=expected_gain,
                tool_cost=spec.tool_cost,
                latency_ms=latency_ms,
                investigation_score=investigation_score,
            )
        )
        evidence_log.append({"tool_name": tool_name, "output": summary})
        used_tools.add(tool_name)

    final_input = {
        "payment": payload,
        "final_hypotheses": [
            {"cause": h.cause, "support_score": h.support_score,
             "contradict_score": h.contradict_score,
             "unresolved_questions": h.unresolved_questions}
            for h in hypotheses
        ],
        "evidence_gathered": evidence_log,
    }
    final_raw = await asyncio.wait_for(
        _generic_llm_json_call(
            _FINALIZE_SYSTEM_PROMPT, final_input, _FINALIZE_SCHEMA, model, api_key
        ),
        timeout=round_timeout_seconds,
    )

    root_cause = RootCause(final_raw["selected_cause"])
    confidence_band = final_raw["confidence_band"]
    confidence = CONFIDENCE_BAND_TO_FLOAT[confidence_band]
    evidence = [Evidence(**e) for e in final_raw["evidence"]]

    # Task S2's adversarial guards apply identically here -- same function,
    # same call shape as the single-call path, so the guard boundary can't
    # be weakened just because diagnosis now takes multiple rounds.
    root_cause, confidence, evidence = apply_adversarial_guards(
        diagnosis_input, root_cause, confidence, evidence
    )

    return InvestigationResult(
        hypotheses=hypotheses,
        selected_cause=root_cause,
        confidence_band=confidence_band,
        confidence=confidence,
        evidence=evidence,
        steps=steps,
    )


async def _generic_llm_json_call(system_prompt, user_content, schema, model, api_key):
    """
    llm_diagnoser's _call_llm_gemini/_call_llm_openai have a FIXED schema/
    prompt baked in for the single-call diagnosis path. The investigator
    needs a different schema/prompt per round, so it goes straight to the
    shared low-level client instead.
    """
    from services.diagnosis_engine.llm_client import gemini_generate_json, strip_additional_properties

    # Only Gemini is wired for the multi-round investigation loop today
    # (Task AGENT1) -- OpenAI's Structured Outputs path could be added the
    # same way llm_diagnoser.py's single-call path supports both, but no
    # OpenAI key has ever been available to build/test that here.
    return await gemini_generate_json(
        system_prompt=system_prompt,
        user_content=user_content,
        response_schema=strip_additional_properties(schema),
        model=model,
        api_key=api_key,
    )
