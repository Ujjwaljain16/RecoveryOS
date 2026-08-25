"""
Implementation of the six failure scenarios (TRD §6, PRD §32).
All scenarios conform to the composable ScenarioModifier protocol.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from simulator.failures.codes import TrueFailureType
from simulator.failures.protocol import PaymentContext, ScenarioModifier


class NormalFailureScenario:
    """
    Scenario A: Normal baseline failure rate (3% stochastic failure).
    Represents routine dropped packets, momentary user aborts, and transient dips.
    """

    name = "normal_baseline"

    def __init__(self, baseline_failure_rate: float = 0.03):
        self.baseline_failure_rate = baseline_failure_rate

    def applies_to(self, context: PaymentContext) -> bool:
        return True

    def get_failure_rate(self, context: PaymentContext) -> float:
        return self.baseline_failure_rate

    def modify_failure_probability(self, base_prob: float, context: PaymentContext) -> float:
        return max(base_prob, self.baseline_failure_rate)

    def determine_true_failure_type(self, context: PaymentContext) -> TrueFailureType:
        return TrueFailureType.TRANSIENT_NETWORK_DROP

    def modify_latent_health(self, base_health: float, context: PaymentContext) -> float:
        return base_health


class BankDegradationScenario:
    """
    Scenario B: Bank Degradation (3% -> 18% spike for a targeted bank during an active window).
    """

    name = "bank_degradation"

    def __init__(
        self,
        target_bank: str = "HDFC",
        spike_rate: float = 0.18,
        window_start: datetime | None = None,
        window_duration_minutes: int = 1440, # 24h default so it covers full simulation
        concentration_bias: float = 0.85,
    ):
        self.target_bank = target_bank
        self.spike_rate = spike_rate
        self.window_start = window_start
        self.window_duration = timedelta(minutes=window_duration_minutes)
        # Real bank degradations are observed against that bank's own actual
        # transaction volume, not a uniform 1/N share of total simulated
        # throughput (this simulator's overall arrival rate is far below a
        # real merchant network's, so an even bank split under-samples any
        # one bank's per-15-minute-bucket volume well below TRD §3.2's
        # anomaly_min_sample_size). While the window is active, bias bank
        # selection toward target_bank (see bank_concentration_bias) so a
        # run's density for that bank approximates its real volume closely
        # enough for the detector's documented default bucket size to
        # actually resolve a signal. This does not touch spike_rate/failure
        # behavior at all -- it only concentrates WHICH bank gets sampled.
        self.concentration_bias = concentration_bias

    def bank_concentration_bias(self, timestamp: datetime) -> tuple[str, float] | None:
        """
        Optional hook PaymentGenerator consults before sampling a payment's
        bank (duck-typed, not part of ScenarioModifier -- most scenarios
        don't target a specific bank/window and have no reason to implement
        it). Returns (target_bank, bias_probability) while this scenario's
        window is active, else None.
        """
        if self.window_start is None:
            return None
        if not (self.window_start <= timestamp <= self.window_start + self.window_duration):
            return None
        return (self.target_bank, self.concentration_bias)

    def applies_to(self, context: PaymentContext) -> bool:
        if context.bank != self.target_bank:
            return False
        if self.window_start is None:
            return True
        return self.window_start <= context.timestamp <= (self.window_start + self.window_duration)

    def get_failure_rate(self, context: PaymentContext) -> float:
        return self.spike_rate

    def modify_failure_probability(self, base_prob: float, context: PaymentContext) -> float:
        return max(base_prob, self.spike_rate)

    def determine_true_failure_type(self, context: PaymentContext) -> TrueFailureType:
        return TrueFailureType.BANK_DEGRADATION_FAIL

    def modify_latent_health(self, base_health: float, context: PaymentContext) -> float:
        return base_health * 0.35


class MultiRailOutageScenario:
    """
    Scenario C: Payment Rail / Multi-Bank Outage (simultaneous degradation across multiple banks).
    """

    name = "multi_rail_outage"

    def __init__(
        self,
        affected_banks: list[str] | None = None,
        outage_failure_rate: float = 0.30,
        window_start: datetime | None = None,
        window_duration_minutes: int = 1440, # 24h default
    ):
        self.affected_banks = affected_banks or ["ICICI", "SBI", "AXIS"]
        self.outage_failure_rate = outage_failure_rate
        self.window_start = window_start
        self.window_duration = timedelta(minutes=window_duration_minutes)

    def applies_to(self, context: PaymentContext) -> bool:
        if context.bank not in self.affected_banks:
            return False
        if self.window_start is None:
            return True
        return self.window_start <= context.timestamp <= (self.window_start + self.window_duration)

    def get_failure_rate(self, context: PaymentContext) -> float:
        return self.outage_failure_rate

    def modify_failure_probability(self, base_prob: float, context: PaymentContext) -> float:
        return max(base_prob, self.outage_failure_rate)

    def determine_true_failure_type(self, context: PaymentContext) -> TrueFailureType:
        return TrueFailureType.MULTI_RAIL_OUTAGE_FAIL

    def modify_latent_health(self, base_health: float, context: PaymentContext) -> float:
        return base_health * 0.15


class TemporaryTimeoutScenario:
    """
    Scenario D: Temporary Timeout (gateway/switch timeout where waiting pays off).
    """

    name = "temporary_timeout"

    def __init__(self, failure_rate: float = 0.08):
        self.failure_rate = failure_rate

    def applies_to(self, context: PaymentContext) -> bool:
        return context.method in ["upi", "netbanking", "card"]

    def get_failure_rate(self, context: PaymentContext) -> float:
        return self.failure_rate

    def modify_failure_probability(self, base_prob: float, context: PaymentContext) -> float:
        return max(base_prob, self.failure_rate)

    def determine_true_failure_type(self, context: PaymentContext) -> TrueFailureType:
        return TrueFailureType.TEMPORARY_GATEWAY_TIMEOUT

    def modify_latent_health(self, base_health: float, context: PaymentContext) -> float:
        return base_health * 0.85


class PermanentFailureScenario:
    """
    Scenario E: Permanent Failure (invalid credentials, expired card, closed account).
    Retry will NEVER succeed — the model must learn not to waste money retrying.
    """

    name = "permanent_failure"

    def __init__(self, failure_rate: float = 0.05):
        self.failure_rate = failure_rate

    def applies_to(self, context: PaymentContext) -> bool:
        return True

    def get_failure_rate(self, context: PaymentContext) -> float:
        return self.failure_rate

    def modify_failure_probability(self, base_prob: float, context: PaymentContext) -> float:
        return max(base_prob, self.failure_rate)

    def determine_true_failure_type(self, context: PaymentContext) -> TrueFailureType:
        if context.method == "card":
            return TrueFailureType.PERMANENT_EXPIRED_INSTRUMENT
        elif context.method == "upi":
            return TrueFailureType.PERMANENT_INVALID_CREDS
        return TrueFailureType.PERMANENT_ACCOUNT_CLOSED

    def modify_latent_health(self, base_health: float, context: PaymentContext) -> float:
        return base_health


class CustomerRepeatFailureScenario:
    """
    Scenario F: Customer-specific repeat failure (single customer repeatedly fails across attempts).
    """

    name = "customer_repeat_failure"

    def __init__(self, failure_rate: float = 0.08):
        self.failure_rate = failure_rate

    def applies_to(self, context: PaymentContext) -> bool:
        return context.customer.latent_propensity_bias < -0.1 or context.customer.lifetime_value_paise < 20_000

    def get_failure_rate(self, context: PaymentContext) -> float:
        return self.failure_rate

    def modify_failure_probability(self, base_prob: float, context: PaymentContext) -> float:
        return max(base_prob, self.failure_rate)

    def determine_true_failure_type(self, context: PaymentContext) -> TrueFailureType:
        return TrueFailureType.CUSTOMER_INSUFFICIENT_FUNDS

    def modify_latent_health(self, base_health: float, context: PaymentContext) -> float:
        return base_health
