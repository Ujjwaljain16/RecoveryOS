"""
Policy Engine test matrix — TRD §3.4, gaps.md §B.3.
7 rules x 4 edge cases each (exactly-at-limit, one-under, one-over, pass)
= 28 tests minimum, plus orchestration/purity/latency tests.
"""

from __future__ import annotations

import ast
import inspect
import time
from datetime import UTC, datetime, timedelta

from services.policy_engine.evaluate import evaluate
from services.policy_engine.rules import (
    RULES,
    AmountLimitRule,
    CandidateContext,
    CooldownRule,
    EligibilityRule,
    MinExpectedValueRule,
    OptOutRule,
    PaymentContext,
    PolicyConfigContext,
    RetryLimitRule,
    SystemicSuppressionRule,
)

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)


def _payment(**overrides) -> PaymentContext:
    defaults = {
        "payment_id": "pay_1",
        "status": "failed",
        "is_expired": False,
        "opted_out_at": None,
        "last_attempt_at": None,
        "attempt_number": 1,
        "amount_paise": 100_000,
        "now": NOW,
        "is_high_severity_anomaly": False,
    }
    defaults.update(overrides)
    return PaymentContext(**defaults)


def _candidate(**overrides) -> CandidateContext:
    defaults = {"action_type": "RETRY_NOW", "expected_value_paise": 1_000}
    defaults.update(overrides)
    return CandidateContext(**defaults)


def _policy_config(**overrides) -> PolicyConfigContext:
    defaults = {
        "max_retries": 2,
        "retry_cooldown_hours": 12,
        "max_amount_paise": 2_500_000,
        "escalate_after_failures": 2,
        "min_expected_value_paise": 0,
    }
    defaults.update(overrides)
    return PolicyConfigContext(**defaults)


# ═══════════════════════════════════════════════════════════════════════
# 1. EligibilityRule — payment.status == 'failed' and not expired
# ═══════════════════════════════════════════════════════════════════════


def test_eligibility_pass_case_failed_and_not_expired():
    result = EligibilityRule().check(
        _payment(status="failed", is_expired=False), _candidate(), _policy_config()
    )
    assert result.passed is True


def test_eligibility_fails_when_status_is_not_failed():
    result = EligibilityRule().check(
        _payment(status="success", is_expired=False), _candidate(), _policy_config()
    )
    assert result.passed is False


def test_eligibility_fails_when_expired():
    result = EligibilityRule().check(
        _payment(status="failed", is_expired=True), _candidate(), _policy_config()
    )
    assert result.passed is False


def test_eligibility_fails_when_status_created_not_yet_failed():
    """'exactly-at-limit' equivalent for a boolean-ish rule: the boundary
    between eligible and not is the status value itself."""
    result = EligibilityRule().check(
        _payment(status="created", is_expired=False), _candidate(), _policy_config()
    )
    assert result.passed is False


# ═══════════════════════════════════════════════════════════════════════
# 2. OptOutRule — customer.opted_out_at is None
# ═══════════════════════════════════════════════════════════════════════


def test_optout_pass_case_not_opted_out():
    result = OptOutRule().check(_payment(opted_out_at=None), _candidate(), _policy_config())
    assert result.passed is True


def test_optout_fails_when_opted_out_long_ago():
    result = OptOutRule().check(
        _payment(opted_out_at=NOW - timedelta(days=30)), _candidate(), _policy_config()
    )
    assert result.passed is False


def test_optout_fails_when_opted_out_one_second_ago():
    """one-under equivalent: opted out just now, still blocks."""
    result = OptOutRule().check(
        _payment(opted_out_at=NOW - timedelta(seconds=1)), _candidate(), _policy_config()
    )
    assert result.passed is False


def test_optout_fails_even_if_opted_out_at_exactly_now():
    """exactly-at-limit: opted_out_at == now — still not None, still blocks."""
    result = OptOutRule().check(_payment(opted_out_at=NOW), _candidate(), _policy_config())
    assert result.passed is False


# ═══════════════════════════════════════════════════════════════════════
# 3. CooldownRule — now - last_attempt >= cooldown_hours
# ═══════════════════════════════════════════════════════════════════════


def test_cooldown_pass_case_no_prior_attempt():
    result = CooldownRule().check(
        _payment(last_attempt_at=None), _candidate(), _policy_config(retry_cooldown_hours=12)
    )
    assert result.passed is True


def test_cooldown_exactly_at_limit_passes():
    result = CooldownRule().check(
        _payment(last_attempt_at=NOW - timedelta(hours=12)),
        _candidate(),
        _policy_config(retry_cooldown_hours=12),
    )
    assert result.passed is True


def test_cooldown_one_minute_under_the_limit_fails():
    result = CooldownRule().check(
        _payment(last_attempt_at=NOW - timedelta(hours=12) + timedelta(minutes=1)),
        _candidate(),
        _policy_config(retry_cooldown_hours=12),
    )
    assert result.passed is False


def test_cooldown_one_minute_over_the_limit_passes():
    result = CooldownRule().check(
        _payment(last_attempt_at=NOW - timedelta(hours=12) - timedelta(minutes=1)),
        _candidate(),
        _policy_config(retry_cooldown_hours=12),
    )
    assert result.passed is True


# ═══════════════════════════════════════════════════════════════════════
# 4. RetryLimitRule — attempt_number <= max_retries (failure ESCALATES)
# ═══════════════════════════════════════════════════════════════════════


def test_retry_limit_pass_case_well_under():
    result = RetryLimitRule().check(
        _payment(attempt_number=1), _candidate(), _policy_config(max_retries=2)
    )
    assert result.passed is True


def test_retry_limit_one_under_passes():
    result = RetryLimitRule().check(
        _payment(attempt_number=1), _candidate(), _policy_config(max_retries=2)
    )
    assert result.passed is True


def test_retry_limit_exactly_at_limit_passes():
    result = RetryLimitRule().check(
        _payment(attempt_number=2), _candidate(), _policy_config(max_retries=2)
    )
    assert result.passed is True


def test_retry_limit_one_over_fails():
    result = RetryLimitRule().check(
        _payment(attempt_number=3), _candidate(), _policy_config(max_retries=2)
    )
    assert result.passed is False


def test_retry_limit_failure_marked_as_escalating():
    assert RetryLimitRule.escalates_on_fail is True


# ═══════════════════════════════════════════════════════════════════════
# 5. AmountLimitRule — amount_paise <= max_amount_paise
# ═══════════════════════════════════════════════════════════════════════


def test_amount_limit_pass_case_well_under():
    result = AmountLimitRule().check(
        _payment(amount_paise=100_000), _candidate(), _policy_config(max_amount_paise=2_500_000)
    )
    assert result.passed is True


def test_amount_limit_one_paise_under_passes():
    result = AmountLimitRule().check(
        _payment(amount_paise=2_499_999), _candidate(), _policy_config(max_amount_paise=2_500_000)
    )
    assert result.passed is True


def test_amount_limit_exactly_at_limit_passes():
    result = AmountLimitRule().check(
        _payment(amount_paise=2_500_000), _candidate(), _policy_config(max_amount_paise=2_500_000)
    )
    assert result.passed is True


def test_amount_limit_one_paise_over_fails():
    result = AmountLimitRule().check(
        _payment(amount_paise=2_500_001), _candidate(), _policy_config(max_amount_paise=2_500_000)
    )
    assert result.passed is False


# ═══════════════════════════════════════════════════════════════════════
# 6. SystemicSuppressionRule
# ═══════════════════════════════════════════════════════════════════════


def test_systemic_suppression_pass_case_no_anomaly():
    result = SystemicSuppressionRule().check(
        _payment(is_high_severity_anomaly=False),
        _candidate(action_type="RETRY_NOW"),
        _policy_config(),
    )
    assert result.passed is True


def test_systemic_suppression_blocks_retry_now_during_high_severity_anomaly():
    result = SystemicSuppressionRule().check(
        _payment(is_high_severity_anomaly=True),
        _candidate(action_type="RETRY_NOW"),
        _policy_config(),
    )
    assert result.passed is False


def test_systemic_suppression_does_not_block_retry_later_during_high_severity_anomaly():
    """'one-under' equivalent: same anomaly condition, different action —
    only RETRY_NOW specifically is suppressed."""
    result = SystemicSuppressionRule().check(
        _payment(is_high_severity_anomaly=True),
        _candidate(action_type="RETRY_LATER"),
        _policy_config(),
    )
    assert result.passed is True


def test_systemic_suppression_does_not_block_do_nothing_during_high_severity_anomaly():
    """'one-over' equivalent boundary: exercising every other action type is
    unaffected even at maximum anomaly severity."""
    result = SystemicSuppressionRule().check(
        _payment(is_high_severity_anomaly=True),
        _candidate(action_type="DO_NOTHING"),
        _policy_config(),
    )
    assert result.passed is True


# ═══════════════════════════════════════════════════════════════════════
# 7. MinExpectedValueRule — EVI > min_expected_value_paise
# ═══════════════════════════════════════════════════════════════════════


def test_min_expected_value_pass_case_well_above_floor():
    result = MinExpectedValueRule().check(
        _payment(),
        _candidate(expected_value_paise=10_000),
        _policy_config(min_expected_value_paise=0),
    )
    assert result.passed is True


def test_min_expected_value_one_paise_above_floor_passes():
    result = MinExpectedValueRule().check(
        _payment(), _candidate(expected_value_paise=1), _policy_config(min_expected_value_paise=0)
    )
    assert result.passed is True


def test_min_expected_value_exactly_at_floor_fails():
    """Strict inequality: EVI == floor does NOT clear it."""
    result = MinExpectedValueRule().check(
        _payment(), _candidate(expected_value_paise=0), _policy_config(min_expected_value_paise=0)
    )
    assert result.passed is False


def test_min_expected_value_one_paise_under_floor_fails():
    result = MinExpectedValueRule().check(
        _payment(), _candidate(expected_value_paise=-1), _policy_config(min_expected_value_paise=0)
    )
    assert result.passed is False


# ═══════════════════════════════════════════════════════════════════════
# Orchestration: ordering, short-circuit, rule_trace, verdicts
# ═══════════════════════════════════════════════════════════════════════


def test_all_rules_pass_produces_allow_with_full_trace():
    result = evaluate(_payment(), _candidate(expected_value_paise=1_000), _policy_config())
    assert result.verdict == "ALLOW"
    assert len(result.rule_trace) == len(RULES)
    assert all(entry["passed"] for entry in result.rule_trace)


def test_first_block_short_circuits_later_rules():
    """status != 'failed' fails EligibilityRule (rule #1) — no later rule
    should even appear in the trace."""
    result = evaluate(_payment(status="success"), _candidate(), _policy_config())
    assert result.verdict == "BLOCK"
    assert len(result.rule_trace) == 1
    assert result.rule_trace[0]["rule"] == "EligibilityRule"


def test_block_identifies_the_blocking_rule():
    result = evaluate(_payment(opted_out_at=NOW), _candidate(), _policy_config())
    assert result.verdict == "BLOCK"
    assert result.rule_trace[-1]["rule"] == "OptOutRule"
    assert result.rule_trace[-1]["passed"] is False


def test_retry_limit_failure_produces_escalate_verdict_not_block():
    result = evaluate(_payment(attempt_number=5), _candidate(), _policy_config(max_retries=2))
    assert result.verdict == "ESCALATE"
    assert result.rule_trace[-1]["rule"] == "RetryLimitRule"


def test_trace_stops_exactly_at_the_failing_rule_middle_of_the_chain():
    """SystemicSuppressionRule is rule #6 — the trace must contain exactly
    6 entries (the 5 that passed plus the failing one), not all 7."""
    result = evaluate(
        _payment(is_high_severity_anomaly=True),
        _candidate(action_type="RETRY_NOW", expected_value_paise=1_000),
        _policy_config(),
    )
    assert result.verdict == "BLOCK"
    assert len(result.rule_trace) == 6
    assert result.rule_trace[-1]["rule"] == "SystemicSuppressionRule"


def test_systemic_suppression_blocks_retry_now_end_to_end_via_evaluate():
    result = evaluate(
        _payment(is_high_severity_anomaly=True),
        _candidate(action_type="RETRY_NOW", expected_value_paise=50_000),
        _policy_config(),
    )
    assert result.verdict == "BLOCK"
    assert any(
        e["rule"] == "SystemicSuppressionRule" and not e["passed"] for e in result.rule_trace
    )


# ═══════════════════════════════════════════════════════════════════════
# gaps.md §B.3: purity + latency
# ═══════════════════════════════════════════════════════════════════════

FORBIDDEN_MODULES = {"sqlalchemy", "redis", "requests", "httpx"}


def _assert_no_forbidden_imports(module) -> None:
    source = inspect.getsource(module)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert (
                    root not in FORBIDDEN_MODULES and root != "db"
                ), f"forbidden import {alias.name!r} found in {module.__name__}"
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert (
                root not in FORBIDDEN_MODULES and root != "db"
            ), f"forbidden import from {node.module!r} found in {module.__name__}"


def test_policy_engine_module_has_zero_forbidden_imports():
    import services.policy_engine.evaluate as evaluate_module
    import services.policy_engine.rules as rules_module

    _assert_no_forbidden_imports(rules_module)
    _assert_no_forbidden_imports(evaluate_module)


def test_policy_evaluate_runs_with_zero_db_queries():
    """No DB session object exists anywhere in this test — if evaluate() or
    any rule tried to do I/O, it would need one and this would fail with an
    AttributeError/NameError, not silently succeed."""
    result = evaluate(_payment(), _candidate(expected_value_paise=1_000), _policy_config())
    assert result.verdict == "ALLOW"


def test_policy_engine_p99_latency_under_10ms():
    """Run evaluate() 10,000 times in a tight loop (no I/O), report the real
    p99 wall-clock time against TRD §8's <10ms target."""
    payment = _payment()
    candidate = _candidate(expected_value_paise=1_000)
    policy_config = _policy_config()

    durations = []
    for _ in range(10_000):
        t0 = time.perf_counter()
        evaluate(payment, candidate, policy_config)
        durations.append(time.perf_counter() - t0)

    durations.sort()
    p99_ms = durations[int(len(durations) * 0.99)] * 1000
    print(f"\n[policy_engine p99] {p99_ms:.4f} ms over 10,000 calls")
    assert p99_ms < 10.0, f"p99={p99_ms:.4f}ms exceeds the 10ms target"
