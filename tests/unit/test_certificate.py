"""
Unit tests for models/recovery/certificate.py's verdict derivation — Task
MD2, pre-Phase-8 audit. The certificate used to hardcode status="PASS"
regardless of the gate results it loaded; these tests prove the derivation
function actually responds to the numbers, including the negative control
this finding specifically needs: a deliberately-failing gate must produce
FAIL, not the old always-PASS literal.
"""

from __future__ import annotations

from models.recovery.certificate import LEAKAGE_AUC_THRESHOLD, derive_leakage_gate_status


def test_certificate_reports_pass_on_a_genuinely_passing_gate():
    train_results = {"leakage_gate": {"ci_hi": LEAKAGE_AUC_THRESHOLD - 0.01}}
    assert derive_leakage_gate_status(train_results) == "PASS"


def test_certificate_reports_fail_on_a_genuinely_failing_gate():
    """
    Negative control: feed a train_results fixture where the leakage gate's
    own CI upper bound sits ABOVE the threshold (visible features are too
    predictive) and confirm the derivation reports FAIL. The pre-fix
    certificate.py had no code path that could ever produce this output —
    it printed the literal string "PASS" no matter what was loaded.
    """
    train_results = {"leakage_gate": {"ci_hi": LEAKAGE_AUC_THRESHOLD + 0.05}}
    assert derive_leakage_gate_status(train_results) == "FAIL"


def test_certificate_gate_status_is_exactly_at_the_threshold_boundary():
    """ci_hi == threshold is not < threshold, so it must FAIL — the gate is
    a strict inequality in train.py's own _run_leakage_gate, and the
    certificate's derivation must match that exactly, not use <=."""
    train_results = {"leakage_gate": {"ci_hi": LEAKAGE_AUC_THRESHOLD}}
    assert derive_leakage_gate_status(train_results) == "FAIL"
