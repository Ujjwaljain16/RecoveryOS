"""
Observation noise pipeline for RecoveryOS Simulator.
Maps true underlying failure states to noisy observed gateway codes and classifications.
Introduces controlled ambiguity (including UNKNOWN labels) reflecting real-world payment telemetry.
"""

from __future__ import annotations

from simulator.core.rng import SimRng
from simulator.failures.codes import ObservedFailure, ObservedFailureClass, TrueFailureType


class ObservationNoisePipeline:
    """
    Simulates real-world imperfect telemetry from payment aggregators and bank switches.
    """

    def __init__(self, rng: SimRng, ambiguity_rate: float = 0.12):
        self.rng = rng
        self.ambiguity_rate = ambiguity_rate

    def observe(self, true_type: TrueFailureType) -> ObservedFailure:
        """
        Produce noisy observed failure telemetry given the true failure root cause.
        """
        if true_type == TrueFailureType.SUCCESS:
            return ObservedFailure(failure_code=None, failure_class=ObservedFailureClass.TEMPORARY)

        # Check if this observation is corrupted by general ambiguity
        is_ambiguous = self.rng.uniform("noise") < self.ambiguity_rate

        if is_ambiguous:
            # 50% chance of UNKNOWN failure class with generic code
            if self.rng.uniform("noise") < 0.5:
                code = self.rng.choice("noise", ["TIMEOUT", "GATEWAY_ERROR", "INTERNAL_ERROR", "UNKNOWN"])
                return ObservedFailure(failure_code=code, failure_class=ObservedFailureClass.UNKNOWN)
            else:
                code = self.rng.choice("noise", ["DECLINED", "ERROR_99", "TRANSACTION_FAILED"])
                return ObservedFailure(failure_code=code, failure_class=ObservedFailureClass.TEMPORARY)

        # Realistic mapping with slight jitter
        if true_type == TrueFailureType.TRANSIENT_NETWORK_DROP:
            code = self.rng.choice("noise", ["NETWORK_ERROR", "CONNECTION_RESET", "TIMEOUT"])
            return ObservedFailure(failure_code=code, failure_class=ObservedFailureClass.TEMPORARY)

        elif true_type == TrueFailureType.BANK_DEGRADATION_FAIL:
            code = self.rng.choice("noise", ["BANK_DOWN", "BANK_UNAVAILABLE", "TIMEOUT", "ACQUIRER_TIMEOUT"])
            # 80% SYSTEMIC, 20% TEMPORARY (noisy categorization by switch)
            f_class = (
                ObservedFailureClass.SYSTEMIC
                if self.rng.uniform("noise") < 0.80
                else ObservedFailureClass.TEMPORARY
            )
            return ObservedFailure(failure_code=code, failure_class=f_class)

        elif true_type == TrueFailureType.MULTI_RAIL_OUTAGE_FAIL:
            code = self.rng.choice("noise", ["RAIL_DOWN", "BANK_DOWN", "SWITCH_OFFLINE"])
            return ObservedFailure(failure_code=code, failure_class=ObservedFailureClass.SYSTEMIC)

        elif true_type == TrueFailureType.TEMPORARY_GATEWAY_TIMEOUT:
            code = self.rng.choice("noise", ["TIMEOUT", "GATEWAY_TIMEOUT", "RESPONSE_TIMEOUT"])
            return ObservedFailure(failure_code=code, failure_class=ObservedFailureClass.TEMPORARY)

        elif true_type == TrueFailureType.PERMANENT_INVALID_CREDS:
            code = self.rng.choice("noise", ["INVALID_CREDS", "AUTH_FAILED", "MPIN_INVALID"])
            return ObservedFailure(failure_code=code, failure_class=ObservedFailureClass.PERMANENT)

        elif true_type == TrueFailureType.PERMANENT_EXPIRED_INSTRUMENT:
            code = self.rng.choice("noise", ["EXPIRED_INSTRUMENT", "CARD_EXPIRED", "TOKEN_REVOKED"])
            return ObservedFailure(failure_code=code, failure_class=ObservedFailureClass.PERMANENT)

        elif true_type == TrueFailureType.PERMANENT_ACCOUNT_CLOSED:
            code = self.rng.choice("noise", ["ACCOUNT_CLOSED", "DO_NOT_HONOR", "INVALID_ACCOUNT"])
            return ObservedFailure(failure_code=code, failure_class=ObservedFailureClass.PERMANENT)

        elif true_type == TrueFailureType.CUSTOMER_INSUFFICIENT_FUNDS:
            code = self.rng.choice("noise", ["INSUFFICIENT_FUNDS", "BALANCE_EXCEEDED", "LIMIT_EXCEEDED"])
            return ObservedFailure(failure_code=code, failure_class=ObservedFailureClass.CUSTOMER_SPECIFIC)

        elif true_type == TrueFailureType.CUSTOMER_AUTH_EXHAUSTED:
            code = self.rng.choice("noise", ["OTP_EXPIRED", "USER_ABORTED", "AUTH_DROPPED"])
            return ObservedFailure(failure_code=code, failure_class=ObservedFailureClass.CUSTOMER_SPECIFIC)

        # Fallback default
        return ObservedFailure(failure_code="UNKNOWN", failure_class=ObservedFailureClass.UNKNOWN)
