"""
Unit tests for services.recovery_engine.orchestrator.resolve_decision_now --
the single authoritative "now" every time-dependent policy rule
(EligibilityRule, CooldownRule, AutopayExecutionWindowRule,
QuietHoursComplianceRule) reads through PaymentContext.now.

gaps.md sec:C.4 -- AutopayExecutionWindowRule/QuietHoursComplianceRule were
being evaluated against the REAL wall clock for every payment, including
synthetic ones. A canonical/evaluation run seeds thousands of payments
spanning simulated days, then makes all of their first decisions within a
few real minutes -- so every one of them was checked against the same real
hour-of-day regardless of when, in the simulated world, it actually failed.
93% of one seed's BLOCKs traced to this single rule, purely because the real
clock happened to be inside an NPCI peak window while the run executed.

These tests monkeypatch recoveryos.clock.utcnow directly (this repo's
established pattern -- see tests/integration/test_decision_e2e.py) rather
than comparing against real datetime.now(UTC), since the whole test session
already pins that seam to a fixed moment (tests/conftest.py's
_pinned_clock_for_determinism) -- asserting against real wall-clock bounds
here would be exactly the kind of time-dependent flakiness that fixture
exists to prevent.

See tests/integration/test_decision_e2e.py for the end-to-end proof that
this is actually wired into the real decision pipeline, and for the exact
discovered bug's regression test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import recoveryos.clock as clock_module
from services.recovery_engine.orchestrator import resolve_decision_now


def test_synthetic_first_decision_uses_the_payments_own_simulated_failure_time(monkeypatch):
    """The bug's exact fix: a synthetic payment's FIRST decision (no prior
    real attempt) must use failed_at, not the real clock."""
    monkeypatch.setattr(clock_module, "utcnow", lambda: datetime(2099, 1, 1, tzinfo=UTC))
    failed_at = datetime(2026, 8, 25, 5, 30, 0, tzinfo=UTC)  # 11:00 IST
    now = resolve_decision_now(is_synthetic=True, failed_at=failed_at, last_attempt_at=None)
    assert now == failed_at


def test_synthetic_subsequent_decision_uses_the_real_clock(monkeypatch):
    """Once a real attempt has executed (last_attempt_at is set), a
    synthetic payment's re-evaluation must use the real clock -- that
    attempt was genuinely scheduled/executed in real time by
    workers/retry_scheduler.py, so CooldownRule's `now - last_attempt_at`
    must stay internally consistent with real elapsed time, not jump back
    to the payment's original failure moment."""
    real_now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(clock_module, "utcnow", lambda: real_now)
    failed_at = datetime(2026, 8, 25, 5, 30, 0, tzinfo=UTC)
    last_attempt_at = datetime(2026, 8, 26, 5, 30, 0, tzinfo=UTC)
    now = resolve_decision_now(
        is_synthetic=True, failed_at=failed_at, last_attempt_at=last_attempt_at
    )
    assert now == real_now


def test_production_payment_always_uses_the_real_clock_even_on_first_decision(monkeypatch):
    """Production safety: non-synthetic (real) traffic must always get the
    real clock, first decision or not -- a real webhook genuinely fails
    right now, so there is no simulated moment to defer to."""
    real_now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(clock_module, "utcnow", lambda: real_now)
    failed_at = datetime(2026, 8, 25, 5, 30, 0, tzinfo=UTC)
    now = resolve_decision_now(is_synthetic=False, failed_at=failed_at, last_attempt_at=None)
    assert now == real_now, "production traffic must never use the simulated failed_at"


def test_synthetic_first_decision_with_no_failed_at_falls_back_to_real_clock(monkeypatch):
    """Edge case: a synthetic payment somehow has no failed_at yet (e.g.
    status='created') -- there is no simulated moment to use, so fall back
    to the real clock rather than crashing or using None."""
    real_now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(clock_module, "utcnow", lambda: real_now)
    now = resolve_decision_now(is_synthetic=True, failed_at=None, last_attempt_at=None)
    assert now == real_now


def test_changing_only_the_simulated_failure_time_changes_the_resolved_now(monkeypatch):
    """Determinism half 2: the simulated timestamp is what actually drives
    the resolved `now` for a first decision -- proven by varying only it
    while the real clock stays fixed."""
    monkeypatch.setattr(clock_module, "utcnow", lambda: datetime(2099, 1, 1, tzinfo=UTC))
    last_attempt_at = None
    now_a = resolve_decision_now(
        is_synthetic=True,
        failed_at=datetime(2026, 8, 25, 4, 0, 0, tzinfo=UTC),  # 09:30 IST -- outside peak
        last_attempt_at=last_attempt_at,
    )
    now_b = resolve_decision_now(
        is_synthetic=True,
        failed_at=datetime(2026, 8, 25, 5, 30, 0, tzinfo=UTC),  # 11:00 IST -- inside peak
        last_attempt_at=last_attempt_at,
    )
    assert now_a != now_b
    assert now_b - now_a == timedelta(hours=1, minutes=30)


def test_real_clock_does_not_affect_synthetic_first_decision_regardless_of_its_value(monkeypatch):
    """Determinism half 1: running the SAME synthetic payment's first
    decision at two different real wall-clock times must resolve to the
    exact same `now` -- the real clock must be irrelevant here."""
    failed_at = datetime(2026, 8, 25, 5, 30, 0, tzinfo=UTC)

    monkeypatch.setattr(clock_module, "utcnow", lambda: datetime(2026, 9, 1, 8, 0, 0, tzinfo=UTC))
    now_1 = resolve_decision_now(is_synthetic=True, failed_at=failed_at, last_attempt_at=None)

    monkeypatch.setattr(clock_module, "utcnow", lambda: datetime(2026, 9, 15, 20, 0, 0, tzinfo=UTC))
    now_2 = resolve_decision_now(is_synthetic=True, failed_at=failed_at, last_attempt_at=None)

    assert now_1 == now_2 == failed_at
