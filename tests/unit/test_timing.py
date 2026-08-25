"""
Unit tests for services/recovery_engine/timing.py — pure functions, no DB.
"""

from __future__ import annotations

from services.recovery_engine.timing import AnomalyContext, expected_recovery_prob_bps

HIGH_ANOMALY = AnomalyContext(
    severity="high", is_anomaly=True, observed_rate=0.18, baseline_rate=0.03
)
MEDIUM_ANOMALY = AnomalyContext(
    severity="medium", is_anomaly=True, observed_rate=0.08, baseline_rate=0.03
)
INSUFFICIENT = AnomalyContext(
    severity="insufficient_data", is_anomaly=False, observed_rate=None, baseline_rate=None
)


def test_no_anomaly_context_leaves_probability_unmodified():
    assert expected_recovery_prob_bps(8200, "RETRY_NOW", None) == 8200


def test_insufficient_data_guard_leaves_probability_unmodified():
    """Same discipline as anomaly.py's own n<30 guard: a thin-sample reading
    must not have a real action-selection consequence."""
    assert expected_recovery_prob_bps(8200, "RETRY_NOW", INSUFFICIENT) == 8200


def test_medium_severity_does_not_penalize():
    """Only HIGH severity (the one that also forms a cohort / triggers
    SystemicSuppressionRule) triggers the penalty — matches TRD §3.2."""
    assert expected_recovery_prob_bps(8200, "RETRY_NOW", MEDIUM_ANOMALY) == 8200


def test_retry_now_penalized_by_real_measured_ratio_during_high_anomaly():
    # observed_success = 1 - 0.18 = 0.82; baseline_success = 1 - 0.03 = 0.97
    # ratio = 0.82 / 0.97 = 0.845360...
    result = expected_recovery_prob_bps(8200, "RETRY_NOW", HIGH_ANOMALY)
    expected_ratio_bps = int(round((0.82 / 0.97) * 10_000))
    expected = (8200 * expected_ratio_bps) // 10_000
    assert result == expected
    assert result < 8200, "RETRY_NOW must be penalized, not left unmodified, during a high anomaly"


def test_retry_later_bypasses_the_penalty_during_high_anomaly():
    assert expected_recovery_prob_bps(8200, "RETRY_LATER", HIGH_ANOMALY) == 8200


def test_alt_route_bypasses_the_penalty_during_high_anomaly():
    assert expected_recovery_prob_bps(8200, "ALT_ROUTE", HIGH_ANOMALY) == 8200


def test_reminder_escalate_do_nothing_are_never_adjusted():
    for action in ("REMINDER", "ESCALATE", "DO_NOTHING"):
        assert expected_recovery_prob_bps(8200, action, HIGH_ANOMALY) == 8200


def test_penalty_never_boosts_even_if_observed_rate_better_than_baseline():
    """Condition 1: clamp to [0, 1.0], penalty-only, never a boost — if a
    bank's observed_rate happens to run BELOW baseline (better than normal)
    in a high-severity-flagged window, RETRY_NOW must not get a probability
    boost above what the certified model itself estimated."""
    inverted = AnomalyContext(
        severity="high", is_anomaly=True, observed_rate=0.01, baseline_rate=0.10
    )
    result = expected_recovery_prob_bps(8200, "RETRY_NOW", inverted)
    assert result <= 8200


def test_zero_baseline_rate_does_not_crash_or_divide_by_zero():
    zero_baseline = AnomalyContext(
        severity="high", is_anomaly=True, observed_rate=0.05, baseline_rate=0.0
    )
    assert expected_recovery_prob_bps(8200, "RETRY_NOW", zero_baseline) == 8200


def test_full_penalty_when_bank_completely_down():
    """observed_rate=1.0 (100% failing) -> observed_success=0 -> ratio=0 ->
    RETRY_NOW's probability floors at 0, never negative."""
    total_outage = AnomalyContext(
        severity="high", is_anomaly=True, observed_rate=1.0, baseline_rate=0.03
    )
    assert expected_recovery_prob_bps(8200, "RETRY_NOW", total_outage) == 0
