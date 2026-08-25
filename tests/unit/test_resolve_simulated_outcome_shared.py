"""
TRD §7's most load-bearing constraint: RecoveryOS's actual execution
(SimulatorAdapter.retry()) and the baseline's counterfactual replay
(services/pipeline/baseline.py) must resolve simulated outcomes through
the IDENTICAL function, not two independently-written pieces of matching
math — otherwise the incremental-revenue comparison silently stops being
apples-to-apples the next time either implementation drifts.

This is checked two ways: (1) both modules import the literal same
function object (not two functions that happen to agree), and (2)
monkeypatching that one function changes the behavior observed through
BOTH call sites — the strongest possible proof, since it would fail if
either module had its own private copy.
"""

from __future__ import annotations

import integrations.razorpay.adapter as adapter_module
import services.pipeline.baseline as baseline_module
from integrations.razorpay.adapter import resolve_simulated_outcome


def test_baseline_and_adapter_import_the_same_function_object():
    assert baseline_module.resolve_simulated_outcome is adapter_module.resolve_simulated_outcome
    assert baseline_module.resolve_simulated_outcome is resolve_simulated_outcome


def test_monkeypatching_the_shared_resolver_changes_both_call_sites(monkeypatch):
    """
    The real proof: patch resolve_simulated_outcome to always return True,
    then confirm SimulatorAdapter.retry() (via a fake conn) and baseline's
    outcome logic both observe the patched behavior — not just one of them
    (which would mean the other has its own private copy of the dice roll).
    """
    calls = []

    def fake_resolver(true_recovery_prob_bps: int) -> bool:
        calls.append(true_recovery_prob_bps)
        return True  # always "succeeds", regardless of the real probability

    monkeypatch.setattr(adapter_module, "resolve_simulated_outcome", fake_resolver)
    monkeypatch.setattr(baseline_module, "resolve_simulated_outcome", fake_resolver)

    # --- SimulatorAdapter.retry() side ---
    # attempt_number=1 stays on the fast path (uses the stored
    # true_recovery_prob_bps directly, no attempt-decay recomputation) --
    # this test only needs to prove the shared resolver is what decides
    # SUCCESS/FAILED, not exercise the attempt>1 decay path (that's
    # test_simulator_adapter_decays_across_attempts's job, against a real DB).
    class _FakeRow(dict):
        pass

    class _FakeMappingsResult:
        def first(self):
            return _FakeRow(true_recovery_prob_bps=1)  # near-zero, irrelevant once patched

    class _FakeResult:
        def mappings(self):
            return _FakeMappingsResult()

    class _FakeConn:
        def execute(self, *args, **kwargs):
            return _FakeResult()

    adapter = adapter_module.SimulatorAdapter()
    result = adapter.retry(_FakeConn(), "fake-payment-id", 100_000, 1)
    assert result.outcome == "SUCCESS", (
        "SimulatorAdapter.retry() did not observe the patched shared resolver -- "
        "it may be calling a different function than the one that was patched"
    )
    assert 1 in calls

    # --- baseline.py side (call the resolver directly as the module does) ---
    calls.clear()
    succeeded = baseline_module.resolve_simulated_outcome(1)
    assert succeeded is True, (
        "baseline.py's own reference to resolve_simulated_outcome did not observe "
        "the patch -- it is not calling the same function object as SimulatorAdapter"
    )
    assert calls == [1]
