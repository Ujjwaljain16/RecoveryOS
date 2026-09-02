"""
Unit tests for services/diagnosis_engine/guards.py and fallback_rules.py —
PRD §37 adversarial cases + gaps.md §A.3. Pure functions, no DB required.
"""

from __future__ import annotations

from services.diagnosis_engine.fallback_rules import (
    FALLBACK_CONFIDENCE_CAP,
    FALLBACK_MAP,
    diagnose_fallback,
)
from services.diagnosis_engine.guards import (
    CONFLICTING_SIGNALS_CONFIDENCE_CAP,
    MISSING_BANK_CONFIDENCE_CAP,
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


def test_missing_bank_metadata_lowers_confidence():
    """
    PRD §37 'Missing information': bank metadata deleted -> confidence must
    be capped low, and the diagnosis must NOT silently abstain to UNKNOWN
    either (it should still say *something* useful from the failure_code,
    just with visibly reduced certainty).
    """
    with_bank = diagnose_fallback(_base_input(bank="HDFC"), reason="ai_diagnoser_not_configured")
    without_bank = diagnose_fallback(_base_input(bank=None), reason="ai_diagnoser_not_configured")

    assert with_bank.root_cause == RootCause.TEMPORARY_BANK_DEGRADATION
    assert (
        without_bank.root_cause == RootCause.TEMPORARY_BANK_DEGRADATION
    ), "a missing bank shouldn't erase the failure_code evidence that IS present"
    assert without_bank.confidence <= MISSING_BANK_CONFIDENCE_CAP
    assert without_bank.confidence < with_bank.confidence
    assert any("bank_metadata_missing" in e.fact for e in without_bank.evidence)


def test_conflicting_signals_flagged_not_silently_resolved():
    """
    PRD §37 'Conflicting information': failure_code=BANK_DOWN implies bank
    trouble, but the anomaly detector reports this exact bank as healthy
    (not anomalous) right now. The diagnosis must surface
    CONFLICTING_SIGNALS explicitly -- never silently pick one side (e.g.
    trusting the failure_code alone, or trusting the anomaly reading alone)
    without recording that the two disagreed.
    """
    conflicting = _base_input(
        bank="HDFC",
        failure_code="BANK_DOWN",
        is_anomaly=False,
        anomaly_scope_type="bank",
        anomaly_scope_entity="HDFC",
        anomaly_severity="low",
    )
    output = diagnose_fallback(conflicting, reason="ai_diagnoser_not_configured")

    assert output.root_cause == RootCause.CONFLICTING_SIGNALS
    assert output.confidence <= CONFLICTING_SIGNALS_CONFIDENCE_CAP
    assert any(
        "conflict:" in e.fact for e in output.evidence
    ), "the conflict itself must be recorded as evidence, not just reflected in a lower number"

    # Sanity: the SAME failure_code with no active anomaly window (nothing
    # to conflict against) must NOT trigger this path.
    no_anomaly_context = _base_input(
        bank="HDFC", failure_code="BANK_DOWN", is_anomaly=False, anomaly_scope_type=None
    )
    non_conflicting = diagnose_fallback(no_anomaly_context, reason="ai_diagnoser_not_configured")
    assert non_conflicting.root_cause != RootCause.CONFLICTING_SIGNALS


def test_unrecognized_failure_code_maps_to_unknown_not_a_guess():
    """PRD §36 Abstention: an unrecognized failure_code must map to UNKNOWN,
    never a confident-sounding fabricated guess."""
    output = diagnose_fallback(
        _base_input(failure_code="SOME_NEW_CODE_NOT_IN_THE_TABLE"),
        reason="ai_diagnoser_not_configured",
    )
    assert output.root_cause == RootCause.UNKNOWN
    assert output.confidence < 0.5


def test_every_fallback_rule_respects_confidence_cap():
    """gaps.md §A.3: no fallback diagnosis may exceed confidence=0.6,
    regardless of which rule in the table fired."""
    for failure_code in list(FALLBACK_MAP.keys()) + [None, "TOTALLY_UNKNOWN_CODE"]:
        output = diagnose_fallback(
            _base_input(failure_code=failure_code), reason="ai_diagnoser_not_configured"
        )
        assert output.confidence <= FALLBACK_CONFIDENCE_CAP
        assert output.is_fallback is True


def test_fallback_output_matches_exact_schema():
    """
    gaps.md §A.3's own named test: fallback output validates against the
    SAME Pydantic model class real AI output uses -- not a same-shaped but
    separately-defined lookalike. `diagnose_fallback` already can't return
    anything else (it's typed to construct `DiagnosisOutput` directly,
    Pydantic would raise on the spot if a field violated the schema), so
    this both proves that by construction (sweeping every real failure_code
    in the mapping table, not just one) and pins the exact shape of what
    gaps.md's own example JSON described, in case a future edit ever swaps
    fallback_rules.py to build some other object by hand.
    """
    import services.diagnosis_engine.fallback_rules as fallback_module
    import services.diagnosis_engine.llm_diagnoser as llm_module
    from services.diagnosis_engine.schemas import DiagnosisOutput, Evidence, RootCause

    # Same class, not a lookalike -- both modules import it from the one
    # place schemas.py defines it (see that module's own docstring).
    assert fallback_module.DiagnosisOutput is DiagnosisOutput
    assert llm_module.DiagnosisOutput is DiagnosisOutput

    for failure_code in list(FALLBACK_MAP.keys()) + [None, "AN_UNMAPPED_CODE"]:
        output = diagnose_fallback(
            _base_input(failure_code=failure_code), reason="ai_diagnoser_timeout"
        )

        assert isinstance(output, DiagnosisOutput)
        assert isinstance(output.root_cause, RootCause)
        assert 0.0 <= output.confidence <= 1.0
        assert output.evidence, "evidence must never be empty (min_length=1 on the schema)"
        assert all(isinstance(e, Evidence) and e.fact and e.source for e in output.evidence)
        assert any(
            e.source == "system" and "fallback_triggered=true" in e.fact for e in output.evidence
        ), "the fallback_triggered=true system fact must always be present (gaps.md §A.3)"
        assert output.model_version == "fallback-rule-v1"
        assert output.is_fallback is True
        # cohort_id is deliberately always None straight out of
        # diagnose_fallback -- diagnoser.py attaches it uniformly afterward
        # for EITHER path (fallback_rules.py's own module docstring).
        assert output.cohort_id is None
