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
    NPCI_AUTOPAY_MAX_ATTEMPTS,
    RBI_EMANDATE_AFA_THRESHOLD_PAISE,
    RULES,
    AIRiskSignalEscalationRule,
    AmountLimitRule,
    AutopayExecutionWindowRule,
    CandidateContext,
    CooldownRule,
    EligibilityRule,
    EMandateRetryComplianceRule,
    MinExpectedValueRule,
    MoneyExposureLimitRule,
    OptOutRule,
    PaymentContext,
    PolicyConfigContext,
    QuietHoursComplianceRule,
    RetryLimitRule,
    SystemicSuppressionRule,
)

# 09:30 IST (04:00 UTC) — deliberately outside BOTH NPCI's Autopay peak
# windows (10:00-13:00, 17:00-21:30 IST) and TRAI's quiet hours
# (21:00-09:00 IST), so the ~30 existing tests below that don't care about
# time-of-day compliance (cooldown/amount/retry-limit logic) don't
# accidentally trip the new EMandateRetryComplianceRule/
# AutopayExecutionWindowRule/QuietHoursComplianceRule (Task COMPLIANCE1) —
# those are tested with their own deliberately-chosen fixtures below.
NOW = datetime(2026, 8, 25, 4, 0, 0, tzinfo=UTC)


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
        "method": "upi",
        "is_high_severity_anomaly": False,
    }
    defaults.update(overrides)
    return PaymentContext(**defaults)


def _candidate(**overrides) -> CandidateContext:
    defaults = {"action_type": "RETRY_NOW", "expected_value_paise": 1_000}
    defaults.update(overrides)
    return CandidateContext(**defaults)


def _at_ist(hour: int, minute: int = 0) -> datetime:
    """A UTC datetime on NOW's date whose IST (UTC+5:30) clock time is
    exactly hour:minute -- for the 3 real-regulatory-compliance rules
    below, which reason in IST wall-clock time."""
    ist_naive = datetime(2026, 8, 25, hour, minute, 0)
    return (ist_naive - timedelta(hours=5, minutes=30)).replace(tzinfo=UTC)


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


def test_eligibility_fails_with_specific_reason_when_already_recovered():
    """Task E1 (Phase 8 Scenario 4 fix): status='recovered' must BLOCK with
    a reason naming the real cause, distinct from the generic 'not failed'
    message any other non-failed status gets -- this is what the
    integration test asserts against in policy_decisions.rule_trace."""
    result = EligibilityRule().check(
        _payment(status="recovered", is_expired=False), _candidate(), _policy_config()
    )
    assert result.passed is False
    assert "already successfully recovered" in result.reason


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
# MoneyExposureLimitRule — pending_exposure_paise + amount_paise <=
# max_money_exposure_paise, for money-moving candidates only (Re-Audit
# MEDIUM finding: recovery_missions.max_money_exposure_paise was computed/
# persisted/displayed but never enforced anywhere until this rule).
# ═══════════════════════════════════════════════════════════════════════


def test_money_exposure_pass_case_no_pending_attempts():
    """The common case: no other attempt outstanding, plenty of room under
    the cap (which orchestrator.py sets to exactly amount_paise in
    practice, but the rule itself is general)."""
    result = MoneyExposureLimitRule().check(
        _payment(amount_paise=100_000, pending_exposure_paise=0, max_money_exposure_paise=100_000),
        _candidate(action_type="RETRY_NOW"),
        _policy_config(),
    )
    assert result.passed is True


def test_money_exposure_exactly_at_cap_passes():
    result = MoneyExposureLimitRule().check(
        _payment(amount_paise=100_000, pending_exposure_paise=0, max_money_exposure_paise=100_000),
        _candidate(action_type="ALT_ROUTE"),
        _policy_config(),
    )
    assert result.passed is True


def test_money_exposure_blocks_when_a_pending_attempt_already_claims_the_cap():
    """The real scenario this rule exists for: one attempt's real order is
    already outstanding (PENDING) for this payment's mission -- a SECOND
    money-moving attempt would push total exposure to 2x the mission's own
    cap (which is always == amount_paise today), so it must be blocked."""
    result = MoneyExposureLimitRule().check(
        _payment(
            amount_paise=100_000, pending_exposure_paise=100_000, max_money_exposure_paise=100_000
        ),
        _candidate(action_type="RETRY_NOW"),
        _policy_config(),
    )
    assert result.passed is False
    assert "100000 paise already outstanding" in result.reason


def test_money_exposure_one_paise_over_cap_fails():
    result = MoneyExposureLimitRule().check(
        _payment(amount_paise=100_001, pending_exposure_paise=0, max_money_exposure_paise=100_000),
        _candidate(action_type="RETRY_NOW"),
        _policy_config(),
    )
    assert result.passed is False


def test_money_exposure_does_not_apply_to_non_money_moving_actions():
    """RETRY_LATER/REMINDER/ESCALATE/DO_NOTHING never create a provider
    order -- exposure can never be affected by them, regardless of how far
    over the cap the payment already is."""
    for action_type in ("RETRY_LATER", "REMINDER", "ESCALATE", "DO_NOTHING"):
        result = MoneyExposureLimitRule().check(
            _payment(
                amount_paise=100_000,
                pending_exposure_paise=999_999_999,
                max_money_exposure_paise=1,
            ),
            _candidate(action_type=action_type),
            _policy_config(),
        )
        assert result.passed is True, f"{action_type} must never be blocked by exposure limits"


def test_money_exposure_defaults_to_unbounded_when_caller_does_not_care():
    """Old call sites (tests, the baseline simulator) that construct
    PaymentContext without pending_exposure_paise/max_money_exposure_paise
    must be unaffected -- the dataclass defaults (0, UNBOUNDED_EXPOSURE_PAISE)
    make this rule trivially pass, matching pre-fix behavior exactly."""
    result = MoneyExposureLimitRule().check(
        _payment(amount_paise=50_000_000_00), _candidate(action_type="RETRY_NOW"), _policy_config()
    )
    assert result.passed is True


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
# AIRiskSignalEscalationRule (Phase 11) — closed-set AI risk_flags signal
# forces ESCALATE, independent of EVI/economics.
# ═══════════════════════════════════════════════════════════════════════


def test_ai_risk_signal_pass_case_no_flags():
    result = AIRiskSignalEscalationRule().check(
        _payment(), _candidate(ai_risk_flags=frozenset()), _policy_config()
    )
    assert result.passed is True


def test_ai_risk_signal_fails_when_a_flag_is_present():
    result = AIRiskSignalEscalationRule().check(
        _payment(),
        _candidate(ai_risk_flags=frozenset({"DUPLICATE_PAYMENT_RISK"})),
        _policy_config(),
    )
    assert result.passed is False


def test_ai_risk_signal_escalates_regardless_of_strongly_positive_evi():
    """Invariant 4 (Phase 11 design doc): a risk flag forces ESCALATE even
    when the candidate's own EVI is strongly positive -- the flag can only
    ever route to the safety rule, never authorize the money-moving action
    it was attached to."""
    result = evaluate(
        _payment(),
        _candidate(
            action_type="RETRY_NOW",
            expected_value_paise=1_000_000,
            ai_risk_flags=frozenset({"HIGH_FRAUD_RISK"}),
        ),
        _policy_config(min_expected_value_paise=0),
    )
    assert result.verdict == "ESCALATE"
    assert result.rule_trace[-1]["rule"] == "AIRiskSignalEscalationRule"


def test_ai_risk_signal_does_not_fire_without_flags_even_with_negative_evi():
    """The inverse: absence of a risk flag must not itself cause an
    escalation -- that's MinExpectedValueRule's job, unaffected by this rule."""
    result = evaluate(
        _payment(),
        _candidate(action_type="RETRY_NOW", expected_value_paise=-1, ai_risk_flags=frozenset()),
        _policy_config(min_expected_value_paise=0),
    )
    assert result.verdict == "BLOCK"
    assert result.rule_trace[-1]["rule"] == "MinExpectedValueRule"


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
    """SystemicSuppressionRule's position in RULES is computed from RULES
    itself, not hardcoded -- a hardcoded position/count here has already
    gone stale once (Re-Audit's MoneyExposureLimitRule addition shifted it
    from #10 to #11 of what's now a 12-rule chain), the exact kind of drift
    this test shouldn't itself be a source of. The real assertion is
    behavioral: the trace must contain exactly the rules up to and
    including the failing one, not the full chain."""
    expected_stop_index = next(
        i for i, rule in enumerate(RULES) if rule.name == "SystemicSuppressionRule"
    )
    result = evaluate(
        _payment(is_high_severity_anomaly=True),
        _candidate(action_type="RETRY_NOW", expected_value_paise=1_000),
        _policy_config(),
    )
    assert result.verdict == "BLOCK"
    assert len(result.rule_trace) == expected_stop_index + 1
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


# ═══════════════════════════════════════════════════════════════════════
# EMandateRetryComplianceRule — real RBI/NPCI regulatory ceilings
# ═══════════════════════════════════════════════════════════════════════


def test_emandate_pass_case_well_within_attempt_and_amount_limits():
    result = EMandateRetryComplianceRule().check(
        _payment(attempt_number=1, amount_paise=100_000),
        _candidate(action_type="RETRY_NOW"),
        _policy_config(),
    )
    assert result.passed is True


def test_emandate_attempt_exactly_at_npci_cap_passes():
    result = EMandateRetryComplianceRule().check(
        _payment(attempt_number=NPCI_AUTOPAY_MAX_ATTEMPTS),
        _candidate(action_type="RETRY_NOW"),
        _policy_config(),
    )
    assert result.passed is True


def test_emandate_attempt_one_over_npci_cap_fails():
    result = EMandateRetryComplianceRule().check(
        _payment(attempt_number=NPCI_AUTOPAY_MAX_ATTEMPTS + 1),
        _candidate(action_type="RETRY_NOW"),
        _policy_config(),
    )
    assert result.passed is False


def test_emandate_amount_exactly_at_rbi_afa_threshold_passes():
    result = EMandateRetryComplianceRule().check(
        _payment(amount_paise=RBI_EMANDATE_AFA_THRESHOLD_PAISE),
        _candidate(action_type="RETRY_NOW"),
        _policy_config(),
    )
    assert result.passed is True


def test_emandate_amount_one_paise_over_rbi_afa_threshold_fails():
    result = EMandateRetryComplianceRule().check(
        _payment(amount_paise=RBI_EMANDATE_AFA_THRESHOLD_PAISE + 1),
        _candidate(action_type="RETRY_NOW"),
        _policy_config(),
    )
    assert result.passed is False


def test_emandate_failure_escalates_not_blocks():
    assert EMandateRetryComplianceRule().escalates_on_fail is True


def test_emandate_does_not_apply_to_non_retry_now_actions():
    """A RETRY_LATER/ALT_ROUTE/etc. candidate is not a silent auto-debit --
    the e-mandate rules don't apply to it at all, regardless of amount."""
    result = EMandateRetryComplianceRule().check(
        _payment(amount_paise=RBI_EMANDATE_AFA_THRESHOLD_PAISE * 10, attempt_number=99),
        _candidate(action_type="RETRY_LATER"),
        _policy_config(),
    )
    assert result.passed is True


def test_emandate_does_not_apply_to_card_payments_regardless_of_amount_or_attempts():
    """RBI/NPCI e-mandate regulations are UPI Autopay/NACH-specific -- a
    card RETRY_NOW must never be blocked by them, no matter how far over
    the NPCI attempt cap or RBI AFA threshold it would otherwise be."""
    result = EMandateRetryComplianceRule().check(
        _payment(
            method="card",
            amount_paise=RBI_EMANDATE_AFA_THRESHOLD_PAISE * 10,
            attempt_number=NPCI_AUTOPAY_MAX_ATTEMPTS + 99,
        ),
        _candidate(action_type="RETRY_NOW"),
        _policy_config(),
    )
    assert result.passed is True


def test_emandate_still_applies_to_upi_payments():
    """Negative control for the method-scoping fix above -- a genuine UPI
    RETRY_NOW over the cap must still be blocked."""
    result = EMandateRetryComplianceRule().check(
        _payment(method="upi", attempt_number=NPCI_AUTOPAY_MAX_ATTEMPTS + 1),
        _candidate(action_type="RETRY_NOW"),
        _policy_config(),
    )
    assert result.passed is False


# ═══════════════════════════════════════════════════════════════════════
# AutopayExecutionWindowRule — NPCI's real non-peak execution windows
# ═══════════════════════════════════════════════════════════════════════


def test_autopay_window_passes_before_10am_ist():
    result = AutopayExecutionWindowRule().check(
        _payment(now=_at_ist(7, 0)), _candidate(action_type="RETRY_NOW"), _policy_config()
    )
    assert result.passed is True


def test_autopay_window_fails_at_11am_ist_peak():
    result = AutopayExecutionWindowRule().check(
        _payment(now=_at_ist(11, 0)), _candidate(action_type="RETRY_NOW"), _policy_config()
    )
    assert result.passed is False


def test_autopay_window_passes_at_1pm_ist_boundary():
    """13:00 IST is the exact START of the 13:00-17:00 non-peak window."""
    result = AutopayExecutionWindowRule().check(
        _payment(now=_at_ist(13, 0)), _candidate(action_type="RETRY_NOW"), _policy_config()
    )
    assert result.passed is True


def test_autopay_window_fails_at_5pm_ist_boundary():
    """17:00 IST is the exact START of the second peak window."""
    result = AutopayExecutionWindowRule().check(
        _payment(now=_at_ist(17, 0)), _candidate(action_type="RETRY_NOW"), _policy_config()
    )
    assert result.passed is False


def test_autopay_window_passes_at_930pm_ist_boundary():
    """21:30 IST is the exact END of the second peak window."""
    result = AutopayExecutionWindowRule().check(
        _payment(now=_at_ist(21, 30)), _candidate(action_type="RETRY_NOW"), _policy_config()
    )
    assert result.passed is True


def test_autopay_window_fails_at_6pm_ist_peak():
    result = AutopayExecutionWindowRule().check(
        _payment(now=_at_ist(18, 0)), _candidate(action_type="RETRY_NOW"), _policy_config()
    )
    assert result.passed is False


def test_autopay_window_passes_at_10pm_ist_after_hours():
    result = AutopayExecutionWindowRule().check(
        _payment(now=_at_ist(22, 0)), _candidate(action_type="RETRY_NOW"), _policy_config()
    )
    assert result.passed is True


def test_autopay_window_does_not_apply_to_non_retry_now_actions():
    result = AutopayExecutionWindowRule().check(
        _payment(now=_at_ist(11, 0)), _candidate(action_type="ALT_ROUTE"), _policy_config()
    )
    assert result.passed is True


def test_autopay_window_never_blocks_card_payments_regardless_of_time_of_day():
    """NPCI's UPI Autopay execution window is a UPI-specific regulation --
    a card RETRY_NOW must pass at every one of the hours a UPI RETRY_NOW
    would be blocked at (found live-testing Phase 10: a real method='card'
    payment was incorrectly blocked by this rule at 17:10 IST)."""
    for hour, minute in ((11, 0), (17, 0), (18, 0)):
        result = AutopayExecutionWindowRule().check(
            _payment(method="card", now=_at_ist(hour, minute)),
            _candidate(action_type="RETRY_NOW"),
            _policy_config(),
        )
        assert result.passed is True, f"card payment wrongly blocked at {hour:02d}:{minute:02d} IST"


def test_autopay_window_never_blocks_netbanking_or_wallet_payments():
    for method in ("netbanking", "wallet"):
        result = AutopayExecutionWindowRule().check(
            _payment(method=method, now=_at_ist(11, 0)),
            _candidate(action_type="RETRY_NOW"),
            _policy_config(),
        )
        assert result.passed is True, f"{method} payment wrongly blocked during an NPCI peak window"


def test_autopay_window_still_blocks_upi_payments_during_peak_window():
    """Negative control for the method-scoping fix above -- a genuine UPI
    RETRY_NOW during an NPCI peak window must still be blocked."""
    result = AutopayExecutionWindowRule().check(
        _payment(method="upi", now=_at_ist(11, 0)),
        _candidate(action_type="RETRY_NOW"),
        _policy_config(),
    )
    assert result.passed is False


# ═══════════════════════════════════════════════════════════════════════
# QuietHoursComplianceRule — TRAI's real quiet-hours rule
# ═══════════════════════════════════════════════════════════════════════


def test_quiet_hours_passes_at_930am_ist_default():
    result = QuietHoursComplianceRule().check(
        _payment(), _candidate(action_type="REMINDER"), _policy_config()
    )
    assert result.passed is True


def test_quiet_hours_fails_at_10pm_ist():
    result = QuietHoursComplianceRule().check(
        _payment(now=_at_ist(22, 0)), _candidate(action_type="REMINDER"), _policy_config()
    )
    assert result.passed is False


def test_quiet_hours_fails_at_3am_ist():
    result = QuietHoursComplianceRule().check(
        _payment(now=_at_ist(3, 0)), _candidate(action_type="REMINDER"), _policy_config()
    )
    assert result.passed is False


def test_quiet_hours_passes_exactly_at_9am_ist_boundary():
    """09:00 IST is the exact END of TRAI's quiet-hours window."""
    result = QuietHoursComplianceRule().check(
        _payment(now=_at_ist(9, 0)), _candidate(action_type="REMINDER"), _policy_config()
    )
    assert result.passed is True


def test_quiet_hours_fails_exactly_at_9pm_ist_boundary():
    """21:00 IST is the exact START of TRAI's quiet-hours window."""
    result = QuietHoursComplianceRule().check(
        _payment(now=_at_ist(21, 0)), _candidate(action_type="REMINDER"), _policy_config()
    )
    assert result.passed is False


def test_quiet_hours_does_not_apply_to_non_reminder_actions():
    result = QuietHoursComplianceRule().check(
        _payment(now=_at_ist(3, 0)), _candidate(action_type="RETRY_NOW"), _policy_config()
    )
    assert result.passed is True


# ═══════════════════════════════════════════════════════════════════════
# End-to-end via evaluate() — the compliance rules actually block a
# real decision, not just their own isolated .check() calls
# ═══════════════════════════════════════════════════════════════════════


def test_evaluate_escalates_when_emandate_attempt_cap_exceeded():
    result = evaluate(
        _payment(attempt_number=NPCI_AUTOPAY_MAX_ATTEMPTS + 1),
        _candidate(action_type="RETRY_NOW", expected_value_paise=1_000),
        _policy_config(
            max_retries=99
        ),  # merchant's OWN policy would allow this -- regulation overrides it
    )
    assert result.verdict == "ESCALATE"
    assert result.rule_trace[-1]["rule"] == "EMandateRetryComplianceRule"


def test_evaluate_blocks_retry_now_during_npci_peak_window():
    result = evaluate(
        _payment(now=_at_ist(11, 0)),
        _candidate(action_type="RETRY_NOW", expected_value_paise=1_000),
        _policy_config(),
    )
    assert result.verdict == "BLOCK"
    assert result.rule_trace[-1]["rule"] == "AutopayExecutionWindowRule"


def test_evaluate_blocks_reminder_during_trai_quiet_hours():
    result = evaluate(
        _payment(now=_at_ist(22, 0)),
        _candidate(action_type="REMINDER", expected_value_paise=1_000),
        _policy_config(),
    )
    assert result.verdict == "BLOCK"
    assert result.rule_trace[-1]["rule"] == "QuietHoursComplianceRule"
