"""
Unit tests for services/pipeline/ledger.py:compute_ledger_entry() — the
function that produces incremental_recovery_paise, Phase 8's headline
number. gaps.md §B.4 discipline: pure integer-paise arithmetic, verified
the same way as services/recovery_engine/evi.py (AST-checked, not just
eyeballed), not assumed because "it looks fine."
"""

from __future__ import annotations

import ast
import inspect

from services.pipeline.ledger import compute_ledger_entry


def test_compute_ledger_entry_uses_only_integer_arithmetic():
    """AST-check compute_ledger_entry() itself: no float literal, no
    float() cast, anywhere in the function."""
    import services.pipeline.ledger as ledger_module

    source = inspect.getsource(ledger_module.compute_ledger_entry)
    tree = ast.parse(source)

    float_literals = [
        node for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, float)
    ]
    assert not float_literals, f"float literal(s) found in compute_ledger_entry: {float_literals}"

    float_casts = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float"
    ]
    assert not float_casts, "float() cast found in compute_ledger_entry"


def test_compute_ledger_entry_matches_hand_computed_value():
    entry = compute_ledger_entry(
        amount_paise=300_000,
        recovery_prob_bps=8500,
        actual_recovery_paise=300_000,
        intervention_cost_paise=100,
        baseline_recovered_amount_paise=0,
        baseline_outcome="NOT_RECOVERED",
    )
    # expected_recovery = 300000 * 8500 // 10000 = 255000
    assert entry.revenue_at_risk_paise == 300_000
    assert entry.expected_recovery_paise == 255_000
    assert entry.actual_recovery_paise == 300_000
    assert entry.incremental_recovery_paise == 300_000  # actual - baseline(0)
    assert entry.net_recovery_paise == 299_900  # actual - cost
    assert entry.baseline_outcome == "NOT_RECOVERED"


def test_compute_ledger_entry_returns_int_types_not_float():
    entry = compute_ledger_entry(
        amount_paise=123_457,
        recovery_prob_bps=3333,
        actual_recovery_paise=0,
        intervention_cost_paise=10,
        baseline_recovered_amount_paise=50_000,
        baseline_outcome="RECOVERED",
    )
    assert isinstance(entry.revenue_at_risk_paise, int)
    assert isinstance(entry.expected_recovery_paise, int)
    assert isinstance(entry.actual_recovery_paise, int)
    assert isinstance(entry.incremental_recovery_paise, int)
    assert isinstance(entry.net_recovery_paise, int)


def test_compute_ledger_entry_incremental_can_be_negative():
    """RecoveryOS did worse than the baseline for this payment -- a real,
    honest possibility the ledger must represent, not clamp away."""
    entry = compute_ledger_entry(
        amount_paise=100_000,
        recovery_prob_bps=5000,
        actual_recovery_paise=0,
        intervention_cost_paise=0,
        baseline_recovered_amount_paise=100_000,
        baseline_outcome="RECOVERED",
    )
    assert entry.incremental_recovery_paise == -100_000


def test_compute_ledger_entry_handles_none_baseline_as_zero():
    """No baseline_runs row exists (e.g. a live, non-synthetic payment) --
    baseline_recovered_amount_paise=None must be treated as 0, not crash."""
    entry = compute_ledger_entry(
        amount_paise=50_000,
        recovery_prob_bps=6000,
        actual_recovery_paise=50_000,
        intervention_cost_paise=0,
        baseline_recovered_amount_paise=None,
        baseline_outcome=None,
    )
    assert entry.incremental_recovery_paise == 50_000
    assert entry.baseline_outcome is None
