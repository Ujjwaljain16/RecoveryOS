"""
Adversarial input guards — PRD §37.

Applied uniformly to BOTH the real LLM path (llm_diagnoser.py, after the
response is parsed and validated) and the deterministic fallback
(fallback_rules.py) — "does the diagnoser handle a bad input sanely" has to
be a property of the diagnosis pipeline as a whole, not an accident of
whichever path happened to run. Implementing this in the rule table only
would mean the LLM path (when it IS reachable) silently skips these checks.

Covers 3 of PRD §37's adversarial cases explicitly (the task's minimum):
  - Missing information (bank=None)   -> lower confidence, don't abstain-guess
  - Conflicting information            -> CONFLICTING_SIGNALS, not a silent pick
  - Systemic degradation (1000 failures from one bank) -> handled by the
    anomaly detector forming a high-severity window + cohort_id BEFORE this
    ever runs; see diagnoser.py's cohort attachment. Nothing extra needed
    here — that case is exercised end-to-end in
    test_systemic_degradation_produces_cohort_diagnosis.
"""

from __future__ import annotations

from services.diagnosis_engine.schemas import DiagnosisInput, Evidence, RootCause

# PRD §37 "Missing information": bank metadata deleted -> "Lower confidence /
# abstain". We lower rather than outright abstain to UNKNOWN — a missing
# bank doesn't erase the failure_code/anomaly evidence that's still
# present, so a capped-confidence guess beats throwing away everything we
# do know. The cap is deliberately below the fallback path's own 0.6 cap
# (gaps.md §A.3) so "missing bank AND fell back to the rule table" is
# visibly lower-confidence than either condition alone.
MISSING_BANK_CONFIDENCE_CAP = 0.4

# PRD §37 "Conflicting information": bank status=healthy but the failure
# code implies bank/systemic trouble. Only failure codes that specifically
# imply a BANK-level (not customer- or gateway-level) problem count as a
# conflict against a "bank healthy" anomaly reading — a TIMEOUT or
# INSUFFICIENT_FUNDS reading doesn't contradict a healthy bank.
_BANK_IMPLYING_FAILURE_CODES = {"BANK_DOWN"}
CONFLICTING_SIGNALS_CONFIDENCE_CAP = 0.35


def apply_adversarial_guards(
    diagnosis_input: DiagnosisInput,
    root_cause: RootCause,
    confidence: float,
    evidence: list[Evidence],
) -> tuple[RootCause, float, list[Evidence]]:
    """
    Post-process a candidate (root_cause, confidence, evidence) — from
    either the LLM or the fallback rule table — against the adversarial
    cases. Returns a possibly-adjusted triple; never raises (an adversarial
    input should degrade the diagnosis's certainty, not crash the pipeline).
    """
    evidence = list(evidence)

    if diagnosis_input.bank is None and root_cause in (
        RootCause.TEMPORARY_BANK_DEGRADATION,
        RootCause.SYSTEMIC_DEGRADATION,
    ):
        confidence = min(confidence, MISSING_BANK_CONFIDENCE_CAP)
        evidence.append(
            Evidence(
                fact="bank_metadata_missing=true — confidence capped, root_cause not abandoned",
                source="system",
            )
        )

    if (
        diagnosis_input.failure_code in _BANK_IMPLYING_FAILURE_CODES
        and diagnosis_input.anomaly_scope_type == "bank"
        and not diagnosis_input.is_anomaly
    ):
        evidence.append(
            Evidence(
                fact=(
                    f"conflict: failure_code={diagnosis_input.failure_code} implies bank "
                    f"degradation, but the anomaly detector reports this bank as "
                    f"severity={diagnosis_input.anomaly_severity!r} (not anomalous)"
                ),
                source="system",
            )
        )
        root_cause = RootCause.CONFLICTING_SIGNALS
        confidence = min(confidence, CONFLICTING_SIGNALS_CONFIDENCE_CAP)

    return root_cause, confidence, evidence
