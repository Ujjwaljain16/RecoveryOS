"""
Structured, schema-validated INPUT and OUTPUT for the AI Diagnoser.

TRD §9 threat model: "AI Diagnoser only receives sanitized, schema-validated
structured fields — never raw free-text fields concatenated into the
prompt; output is schema-validated (Pydantic) before use, rejected if
malformed." This module IS that boundary — `DiagnosisInput` is the entire
surface the LLM prompt is built from (llm_diagnoser.py never touches a raw
DB row or event payload directly), and `DiagnosisOutput` is what every
diagnosis path (LLM success, fallback, adversarial guard) must produce,
so downstream code never special-cases "was this AI or fallback"
(gaps.md §A.3).

PII note (TRD §9): no customer name, contact, or other PII field exists
anywhere on this model — only `customer_is_returning` (bool) and
`customer_prior_recovery_rate` (aggregate stat), matching "Customer PII
never enters the diagnosis pipeline — only customer_id, is_returning, and
aggregate stats are passed." `customer_id` itself is deliberately NOT
included either: the diagnoser reasons about a payment's *situation*, not
which customer it is — even an opaque id is more identity than this layer
needs.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class RootCause(str, Enum):
    """Closed enum — the diagnoser must abstain to UNKNOWN rather than invent
    a value outside this set (PRD §36 "Abstention": unknown situations should
    result in UNKNOWN rather than fabricated certainty)."""

    TEMPORARY_BANK_DEGRADATION = "temporary_bank_degradation"
    SYSTEMIC_DEGRADATION = "systemic_degradation"
    PERMANENT_FAILURE = "permanent_failure"
    CUSTOMER_SPECIFIC = "customer_specific"
    # Adversarial case (PRD §37 "Conflicting information"): bank telemetry
    # says healthy but the failure code implies systemic trouble (or vice
    # versa). The PRD's expected behavior is explicitly "investigate
    # conflict" — NOT silently picking one side. This value names that
    # outcome instead of forcing a guess into one of the other four.
    CONFLICTING_SIGNALS = "conflicting_signals"
    UNKNOWN = "unknown"


class Evidence(BaseModel):
    """One structured fact the diagnosis cites — gaps.md §A.3's exact shape.
    `fact` must be a concrete, checkable statement built from DiagnosisInput
    fields (e.g. "bank_x observed_rate=14.8%, baseline=3.1%, z=7.1"), not
    free-form LLM prose — this is what makes the audit explorer's "why this
    diagnosis" screen (PRD §48) a real grounding check instead of a
    fabricated-looking summary."""

    fact: str
    source: str  # e.g. "payment_metadata", "anomaly_window", "system"


class DiagnosisInput(BaseModel):
    """
    The ENTIRE surface available to the LLM prompt and to the deterministic
    fallback. Every field here is either a closed enum, a bounded number, or
    a bool — there is no free-text field for a malicious failure_code string
    to be concatenated into a prompt as an injection vector (TRD §9). This
    model is built exclusively from diagnoser_role-readable columns (see
    diagnoser.py:build_diagnosis_input) plus the output of the anomaly
    detector — never from `ground_truth_recoverable` or any simulator latent
    field, which diagnoser_role has zero grant on at the DB level regardless.
    """

    payment_id: str
    amount_paise: int = Field(gt=0)
    method: str
    bank: str | None = None
    failure_code: str | None = None
    failure_class: str | None = None
    attempt_number: int = Field(default=1, ge=1)
    customer_is_returning: bool | None = None
    customer_prior_recovery_rate: float | None = Field(default=None, ge=0.0, le=1.0)

    # Anomaly context (from services.risk_engine.anomaly) — None if no
    # anomaly window exists yet for this payment's bank/method at this time.
    is_anomaly: bool = False
    anomaly_severity: str | None = None  # insufficient_data|low|medium|high
    anomaly_scope_type: str | None = None  # bank|method|merchant
    anomaly_scope_entity: str | None = None
    anomaly_z_score: float | None = None
    anomaly_observed_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    anomaly_baseline_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    # The exact bucket the anomaly window was computed for — needed to
    # re-derive the SAME cohort_id services.risk_engine.anomaly.derive_cohort_id
    # produced for this window (cohort_id isn't stored on anomaly_windows
    # itself; see that module's docstring for why).
    anomaly_time_bucket: datetime | None = None


class DiagnosisOutput(BaseModel):
    """
    The ONLY shape a diagnosis is ever produced in — real AI output and the
    deterministic fallback both construct this SAME class (gaps.md §A.3:
    "structurally identical shape... so downstream code never has to
    special-case was this AI or fallback"). `is_fallback` and
    `model_version` are the only fields whose *meaning* differs; the schema
    itself never does.
    """

    root_cause: RootCause
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(min_length=1)
    cohort_id: str | None = None
    model_version: str
    is_fallback: bool

    @model_validator(mode="after")
    def _fallback_confidence_cap(self) -> DiagnosisOutput:
        # Belt-and-suspenders: the authoritative cap is applied explicitly in
        # fallback_rules.py (documented there with the reasoning); this
        # validator just makes it impossible for ANY code path — a future
        # bug, a copy-pasted rule — to construct a fallback DiagnosisOutput
        # that violates gaps.md §A.3's confidence cap, even accidentally.
        # model_validator(mode="after"), not field_validator: needs both
        # is_fallback and confidence, and field order alone doesn't
        # guarantee is_fallback is already validated when confidence is.
        if self.is_fallback and self.confidence > 0.6:
            raise ValueError(
                f"fallback diagnoses must not exceed confidence=0.6 (gaps.md §A.3), "
                f"got {self.confidence}"
            )
        return self
