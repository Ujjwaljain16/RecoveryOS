"""
Policy Engine rule DSL — TRD §3.4, gaps.md §B.3.

ALL 7 rules are pure functions of already-hydrated dataclasses. Zero I/O:
no db, no sqlalchemy, no redis, no requests, no httpx import anywhere in
this file — enforced both by convention here AND by
test_policy_engine_module_has_zero_forbidden_imports (AST-parses this exact
file). If a rule needs a piece of data (last_attempt_at, current anomaly
severity, opt-out status), the CALLER fetches it once and packs it into
PaymentContext/CandidateContext/PolicyConfigContext BEFORE evaluate() runs —
see gaps.md §B.3's exact rationale for why this has to be structural, not
just a docstring warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class PaymentContext:
    """
    Every fact a rule could need about the payment, pre-fetched by the
    caller. `now` is passed in rather than read from a clock inside a rule —
    same purity reasoning: a rule must be a deterministic function of its
    inputs, not of wall-clock time at evaluation.
    """

    payment_id: str
    status: str  # created|authorized|failed|success|expired
    is_expired: bool
    opted_out_at: datetime | None  # None = not opted out
    last_attempt_at: datetime | None  # None = never attempted
    attempt_number: int  # attempt number this candidate would be, if allowed
    amount_paise: int
    now: datetime
    # Pre-fetched anomaly state for this payment's bank/method (Phase 4's
    # anomaly detector) — packed in here rather than fetched by
    # SystemicSuppressionRule itself.
    is_high_severity_anomaly: bool


@dataclass(frozen=True)
class CandidateContext:
    action_type: str  # RETRY_NOW|RETRY_LATER|ALT_ROUTE|REMINDER|ESCALATE|DO_NOTHING
    expected_value_paise: int


@dataclass(frozen=True)
class PolicyConfigContext:
    max_retries: int
    retry_cooldown_hours: int
    max_amount_paise: int
    escalate_after_failures: int
    min_expected_value_paise: int


@dataclass(frozen=True)
class RuleResult:
    passed: bool
    reason: str


class PolicyRule:
    """Base shape every rule follows — name, whether failure escalates
    (verdict=ESCALATE) vs blocks (verdict=BLOCK), and a pure check()."""

    name: str = "PolicyRule"
    escalates_on_fail: bool = False

    def check(
        self,
        payment: PaymentContext,
        candidate: CandidateContext,
        policy_config: PolicyConfigContext,
    ) -> RuleResult:
        raise NotImplementedError


class EligibilityRule(PolicyRule):
    """payment.status == 'failed' and not expired."""

    name = "EligibilityRule"

    def check(self, payment, candidate, policy_config) -> RuleResult:
        if payment.status != "failed":
            return RuleResult(False, f"payment.status={payment.status!r} is not 'failed'")
        if payment.is_expired:
            return RuleResult(False, "payment has expired")
        return RuleResult(True, "payment is failed and not expired")


class OptOutRule(PolicyRule):
    """customer.opted_out_at is None."""

    name = "OptOutRule"

    def check(self, payment, candidate, policy_config) -> RuleResult:
        if payment.opted_out_at is not None:
            return RuleResult(False, f"customer opted out at {payment.opted_out_at.isoformat()}")
        return RuleResult(True, "customer has not opted out")


class CooldownRule(PolicyRule):
    """now - last_attempt >= cooldown_hours."""

    name = "CooldownRule"

    def check(self, payment, candidate, policy_config) -> RuleResult:
        if payment.last_attempt_at is None:
            return RuleResult(True, "no prior attempt — cooldown not applicable")
        elapsed = payment.now - payment.last_attempt_at
        required = timedelta(hours=policy_config.retry_cooldown_hours)
        if elapsed >= required:
            return RuleResult(True, f"elapsed={elapsed} >= cooldown={required}")
        return RuleResult(False, f"elapsed={elapsed} < cooldown={required}")


class RetryLimitRule(PolicyRule):
    """attempt_number <= max_retries. Failure ESCALATES (not a plain BLOCK):
    exceeding the retry limit means stop auto-retrying and route to a human/
    escalation flow (TRD §2's policy_configs.escalate_after_failures exists
    for exactly this), not just silently drop the payment."""

    name = "RetryLimitRule"
    escalates_on_fail = True

    def check(self, payment, candidate, policy_config) -> RuleResult:
        if payment.attempt_number <= policy_config.max_retries:
            return RuleResult(
                True,
                f"attempt_number={payment.attempt_number} <= max_retries={policy_config.max_retries}",
            )
        return RuleResult(
            False,
            f"attempt_number={payment.attempt_number} > max_retries={policy_config.max_retries}",
        )


class AmountLimitRule(PolicyRule):
    """amount_paise <= max_amount_paise."""

    name = "AmountLimitRule"

    def check(self, payment, candidate, policy_config) -> RuleResult:
        if payment.amount_paise <= policy_config.max_amount_paise:
            return RuleResult(
                True,
                f"amount_paise={payment.amount_paise} <= max_amount_paise={policy_config.max_amount_paise}",
            )
        return RuleResult(
            False,
            f"amount_paise={payment.amount_paise} > max_amount_paise={policy_config.max_amount_paise}",
        )


class SystemicSuppressionRule(PolicyRule):
    """If cohort is SYSTEMIC (high-severity anomaly active) and
    action == RETRY_NOW -> BLOCK, suggest RETRY_LATER."""

    name = "SystemicSuppressionRule"

    def check(self, payment, candidate, policy_config) -> RuleResult:
        if payment.is_high_severity_anomaly and candidate.action_type == "RETRY_NOW":
            return RuleResult(
                False,
                "high-severity systemic anomaly active for this bank/method — "
                "RETRY_NOW suppressed, consider RETRY_LATER",
            )
        return RuleResult(True, "no systemic suppression applies to this action")


class MinExpectedValueRule(PolicyRule):
    """EVI > min_expected_value_paise."""

    name = "MinExpectedValueRule"

    def check(self, payment, candidate, policy_config) -> RuleResult:
        if candidate.expected_value_paise > policy_config.min_expected_value_paise:
            return RuleResult(
                True,
                f"expected_value_paise={candidate.expected_value_paise} > "
                f"floor={policy_config.min_expected_value_paise}",
            )
        return RuleResult(
            False,
            f"expected_value_paise={candidate.expected_value_paise} <= "
            f"floor={policy_config.min_expected_value_paise}",
        )


# Ordered, short-circuit on first BLOCK/ESCALATE — TRD §3.4's exact list.
RULES: tuple[PolicyRule, ...] = (
    EligibilityRule(),
    OptOutRule(),
    CooldownRule(),
    RetryLimitRule(),
    AmountLimitRule(),
    SystemicSuppressionRule(),
    MinExpectedValueRule(),
)
