"""
Deterministic fallback diagnosis — gaps.md §A.3.

Exact schema, exact mapping table, exact confidence cap as specified there.
Produces the SAME `DiagnosisOutput` class the real LLM path produces (see
schemas.py) — downstream code never has to special-case "was this AI or
fallback," only read `is_fallback`.

Cohort attachment (the systemic-degradation override + cohort_id) is
deliberately NOT done here — it's applied once, uniformly, in
diagnoser.py's orchestrator to whatever DiagnosisOutput comes back from
EITHER this fallback or the real LLM path. Doing it here too would mean two
copies of the same "if high-severity anomaly, this is a cohort" rule to
keep in sync.
"""

from __future__ import annotations

from services.diagnosis_engine.guards import apply_adversarial_guards
from services.diagnosis_engine.schemas import DiagnosisInput, DiagnosisOutput, Evidence, RootCause

MODEL_VERSION = "fallback-rule-v1"

# gaps.md §A.3: confidence is capped at 0.6 max regardless of the rule
# matched — fallback should never claim higher certainty than a rule-table
# lookup deserves. Hardcoded, not tunable per merchant.
FALLBACK_CONFIDENCE_CAP = 0.6

# gaps.md §A.3's exact mapping table.
FALLBACK_MAP: dict[str, tuple[str, float]] = {
    "TIMEOUT": ("temporary_bank_degradation", 0.55),
    "BANK_DOWN": ("systemic_degradation", 0.60),
    "INVALID_CREDS": ("permanent_failure", 0.60),
    "INSUFFICIENT_FUNDS": ("customer_specific", 0.50),
    "EXPIRED_INSTRUMENT": ("permanent_failure", 0.55),
    # Default / unrecognized failure_code: abstain to UNKNOWN rather than
    # guess (PRD §36 "Abstention" — proven directly by
    # test_unrecognized_failure_code_maps_to_unknown_not_a_guess).
    "_DEFAULT": ("unknown", 0.30),
}


def diagnose_fallback(diagnosis_input: DiagnosisInput, reason: str) -> DiagnosisOutput:
    """
    Produce a deterministic diagnosis from the rule table — called whenever
    the real AI Diagnoser times out, errors, or is unconfigured (no API key).

    `reason` is a short machine string (e.g. "ai_diagnoser_timeout",
    "ai_diagnoser_error: <exc class name>", "ai_diagnoser_not_configured")
    recorded as a system evidence fact so the audit explorer can show
    exactly why the fallback fired, not just that it did.

    cohort_id is intentionally always None here — see module docstring;
    diagnoser.py attaches it uniformly after this returns.
    """
    root_cause_value, base_confidence = FALLBACK_MAP.get(
        diagnosis_input.failure_code or "", FALLBACK_MAP["_DEFAULT"]
    )
    root_cause = RootCause(root_cause_value)

    evidence = [
        Evidence(fact=f"failure_code={diagnosis_input.failure_code}", source="payment_metadata"),
        Evidence(fact=f"fallback_triggered=true, reason={reason}", source="system"),
    ]

    root_cause, confidence, evidence = apply_adversarial_guards(
        diagnosis_input, root_cause, base_confidence, evidence
    )
    confidence = min(confidence, FALLBACK_CONFIDENCE_CAP)

    return DiagnosisOutput(
        root_cause=root_cause,
        confidence=confidence,
        evidence=evidence,
        cohort_id=None,
        model_version=MODEL_VERSION,
        is_fallback=True,
    )
