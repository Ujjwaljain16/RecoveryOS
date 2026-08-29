"""
Policy Engine rule DSL — TRD §3.4, gaps.md §B.3.

ALL rules (10, as of Task COMPLIANCE1 — 7 original + 3 real regulatory
compliance rules) are pure functions of already-hydrated dataclasses. Zero I/O:
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
from datetime import UTC, datetime, timedelta


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
    # upi|card|netbanking|wallet — needed so EMandateRetryComplianceRule/
    # AutopayExecutionWindowRule (both real NPCI/RBI UPI Autopay
    # regulations) can scope themselves to UPI, instead of applying a
    # UPI-specific regulatory ceiling to every payment method.
    method: str
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
    """
    payment.status == 'failed' and not expired.

    Task E1 (Phase 8 Scenario 4 fix): this is also what makes "the payment
    was already successfully recovered" unconditionally BLOCK, independent
    of cooldown/attempt-count -- services/pipeline/ledger.py sets
    payment.status='recovered' on a real SUCCESS outcome, and this rule
    runs FIRST (before CooldownRule), so the rule_trace correctly names
    this as the reason rather than a coincidental CooldownRule catch that
    would stop applying once enough wall-clock time has passed.
    """

    name = "EligibilityRule"

    def check(self, payment, candidate, policy_config) -> RuleResult:
        if payment.status == "recovered":
            return RuleResult(False, "payment already successfully recovered")
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


# ─── Real regulatory compliance rules (Task COMPLIANCE1) ────────────────────
# Distinct from RetryLimitRule/AmountLimitRule above, which are RecoveryOS's
# own configurable internal risk policy (a merchant's policy_config can set
# max_retries/max_amount_paise to whatever it wants). These three are HARD,
# non-negotiable regulatory ceilings that apply regardless of policy_config
# — cited to the actual RBI/NPCI/TRAI rule, not invented thresholds.
#
# IST = UTC+5:30, computed here rather than stored on PaymentContext: `now`
# is already a real, caller-supplied datetime (gaps.md §B.3's purity
# discipline — no rule reads wall-clock time itself), timezone conversion
# from it is still a pure function of that input.
IST_OFFSET = timedelta(hours=5, minutes=30)

# RBI Digital Payments — E-Mandate Framework, 2026 (circular
# RBI/DPSS/2026-27/396, dated 2026-04-21, consolidating the e-mandate AFA
# rules first introduced 2019 and revised since): recurring transactions
# may be processed WITHOUT Additional Factor of Authentication only up to
# Rs 15,000 per transaction. A silent auto-debit retry (RETRY_NOW) above
# this threshold is not AFA-exempt and cannot proceed unattended.
RBI_EMANDATE_AFA_THRESHOLD_PAISE = 1_500_000  # Rs 15,000

# NPCI's UPI Autopay mandate retry rules, effective 2025-08-01: a maximum
# of 4 total attempts per mandate per billing cycle (1 original execution +
# 3 retries) — a real regulatory ceiling on top of (and generally stricter
# than) whatever a merchant's own policy_config.max_retries allows.
NPCI_AUTOPAY_MAX_ATTEMPTS = 4

# NPCI's UPI Autopay non-peak execution windows, effective 2025-08-01:
# autopay debits are permitted only before 10:00, between 13:00-17:00, and
# after 21:30 IST — (start_hour, start_minute, end_hour, end_minute) peak
# windows where execution is NOT permitted.
NPCI_AUTOPAY_PEAK_WINDOWS_IST = (
    (10, 0, 13, 0),
    (17, 0, 21, 30),
)

# TRAI's Telecom Commercial Communications Customer Preference Regulations
# (TCCCPR), 2018, as amended February 2025: no promotional/commercial voice
# call or SMS between 21:00 and 09:00 IST, regardless of DND registration.
TRAI_QUIET_HOURS_START_IST = 21
TRAI_QUIET_HOURS_END_IST = 9


def _to_ist(now: datetime) -> datetime:
    return now.astimezone(UTC) + IST_OFFSET


class EMandateRetryComplianceRule(PolicyRule):
    """
    RBI/NPCI e-mandate regulations (real, cited above) -- applies only to
    RETRY_NOW, the silent auto-debit-retry action a real UPI Autopay/NACH
    mandate retry corresponds to, AND only to method='upi' -- these are
    UPI Autopay/NACH-specific regulatory ceilings, not a general retry
    limit, so a card/netbanking/wallet RETRY_NOW must never be blocked by
    them (found live-testing Phase 10: a method='card' payment was
    incorrectly blocked by this rule's sibling, AutopayExecutionWindowRule,
    before this fix). escalates_on_fail=True: exceeding a REGULATORY
    ceiling (not an internal risk preference) should stop and route to a
    human/compliance review, same semantics as RetryLimitRule's own
    internal cap.
    """

    name = "EMandateRetryComplianceRule"
    escalates_on_fail = True

    def check(self, payment, candidate, policy_config) -> RuleResult:
        if candidate.action_type != "RETRY_NOW":
            return RuleResult(True, "not a RETRY_NOW action — e-mandate rules don't apply")
        if payment.method != "upi":
            return RuleResult(
                True, f"method={payment.method!r}, not upi — e-mandate rules don't apply"
            )
        if payment.attempt_number > NPCI_AUTOPAY_MAX_ATTEMPTS:
            return RuleResult(
                False,
                f"attempt_number={payment.attempt_number} exceeds NPCI's regulatory cap of "
                f"{NPCI_AUTOPAY_MAX_ATTEMPTS} attempts per mandate per cycle "
                f"(1 original + 3 retries, effective 2025-08-01)",
            )
        if payment.amount_paise > RBI_EMANDATE_AFA_THRESHOLD_PAISE:
            return RuleResult(
                False,
                f"amount_paise={payment.amount_paise} exceeds the RBI e-mandate AFA-exempt "
                f"threshold of {RBI_EMANDATE_AFA_THRESHOLD_PAISE} paise (Rs 15,000, RBI/DPSS/"
                f"2026-27/396) — a silent RETRY_NOW auto-debit above this threshold requires "
                f"Additional Factor of Authentication and cannot proceed unattended",
            )
        return RuleResult(True, "within NPCI's attempt cap and RBI's AFA-exempt threshold")


class AutopayExecutionWindowRule(PolicyRule):
    """NPCI's UPI Autopay non-peak execution window (real, cited above,
    effective 2025-08-01) — RETRY_NOW may execute only before 10:00,
    between 13:00-17:00, or after 21:30 IST. Scoped to method='upi' only
    -- this is a UPI Autopay-specific execution-window regulation, not a
    general time-of-day retry restriction, so it must never block a
    card/netbanking/wallet RETRY_NOW."""

    name = "AutopayExecutionWindowRule"

    def check(self, payment, candidate, policy_config) -> RuleResult:
        if candidate.action_type != "RETRY_NOW":
            return RuleResult(True, "not a RETRY_NOW action — execution-window rule doesn't apply")
        if payment.method != "upi":
            return RuleResult(
                True, f"method={payment.method!r}, not upi — execution-window rule doesn't apply"
            )
        ist_now = _to_ist(payment.now)
        minutes = ist_now.hour * 60 + ist_now.minute
        in_peak = any(
            (start_h * 60 + start_m) <= minutes < (end_h * 60 + end_m)
            for start_h, start_m, end_h, end_m in NPCI_AUTOPAY_PEAK_WINDOWS_IST
        )
        if in_peak:
            return RuleResult(
                False,
                f"now={ist_now.strftime('%H:%M')} IST falls inside an NPCI peak window — UPI "
                f"Autopay execution (effective 2025-08-01) is permitted only before 10:00, "
                f"13:00-17:00, or after 21:30 IST",
            )
        return RuleResult(
            True, f"now={ist_now.strftime('%H:%M')} IST is within a non-peak execution window"
        )


class QuietHoursComplianceRule(PolicyRule):
    """TRAI TCCCPR quiet-hours rule (real, cited above): no promotional/
    commercial communication between 21:00 and 09:00 IST. Applies to
    REMINDER, the only customer-contact action in this system."""

    name = "QuietHoursComplianceRule"

    def check(self, payment, candidate, policy_config) -> RuleResult:
        if candidate.action_type != "REMINDER":
            return RuleResult(True, "not a REMINDER action — quiet-hours rule doesn't apply")
        ist_now = _to_ist(payment.now)
        in_quiet_hours = (
            ist_now.hour >= TRAI_QUIET_HOURS_START_IST or ist_now.hour < TRAI_QUIET_HOURS_END_IST
        )
        if in_quiet_hours:
            return RuleResult(
                False,
                f"now={ist_now.strftime('%H:%M')} IST is within TRAI's mandated quiet hours "
                f"({TRAI_QUIET_HOURS_START_IST:02d}:00-{TRAI_QUIET_HOURS_END_IST:02d}:00) — "
                f"commercial communication is prohibited in this window",
            )
        return RuleResult(
            True, f"now={ist_now.strftime('%H:%M')} IST is outside TRAI's quiet hours"
        )


class SystemicSuppressionRule(PolicyRule):
    """If cohort is SYSTEMIC (high-severity anomaly active) and
    action == RETRY_NOW -> BLOCK, suggest RETRY_LATER.

    In the live pipeline this rule never actually fires: EVI's
    SYSTEMIC_RISK_PENALTY_PAISE and timing.py's probability haircut
    (services/recovery_engine/evi.py, timing.py) already make RETRY_LATER's
    EVI provably beat RETRY_NOW's EVI whenever is_high_severity_anomaly is
    true, for any amount or degradation severity — so NBA selection never
    offers this rule a RETRY_NOW candidate to block in the first place. This
    is deliberate layered defense (three independent mechanisms enforcing
    the same TRD §3.1 requirement), not dead code — see
    tests/integration/test_systemic_suppression_organic.py for the proof
    and the reasoning for keeping it as a backstop anyway."""

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
    EMandateRetryComplianceRule(),
    AutopayExecutionWindowRule(),
    QuietHoursComplianceRule(),
    SystemicSuppressionRule(),
    MinExpectedValueRule(),
)
