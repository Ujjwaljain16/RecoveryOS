"""
Failure codes, true failure types, and observed failure classifications.
Separates True Underlying Reality from Noisy Observed Telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TrueFailureType(str, Enum):
    """The underlying ground-truth reality of why a payment failed (or succeeded)."""
    SUCCESS = "SUCCESS"
    TRANSIENT_NETWORK_DROP = "TRANSIENT_NETWORK_DROP"
    BANK_DEGRADATION_FAIL = "BANK_DEGRADATION_FAIL"
    MULTI_RAIL_OUTAGE_FAIL = "MULTI_RAIL_OUTAGE_FAIL"
    TEMPORARY_GATEWAY_TIMEOUT = "TEMPORARY_GATEWAY_TIMEOUT"
    PERMANENT_INVALID_CREDS = "PERMANENT_INVALID_CREDS"
    PERMANENT_EXPIRED_INSTRUMENT = "PERMANENT_EXPIRED_INSTRUMENT"
    PERMANENT_ACCOUNT_CLOSED = "PERMANENT_ACCOUNT_CLOSED"
    CUSTOMER_INSUFFICIENT_FUNDS = "CUSTOMER_INSUFFICIENT_FUNDS"
    CUSTOMER_AUTH_EXHAUSTED = "CUSTOMER_AUTH_EXHAUSTED"


class ObservedFailureClass(str, Enum):
    """Failure classes defined in TRD §2."""
    TEMPORARY = "TEMPORARY"
    PERMANENT = "PERMANENT"
    CUSTOMER_SPECIFIC = "CUSTOMER_SPECIFIC"
    SYSTEMIC = "SYSTEMIC"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ObservedFailure:
    """The noisy telemetry that the external banking rails / payment gateways return."""
    failure_code: str | None           # e.g., TIMEOUT, BANK_DOWN, INVALID_CREDS, UNKNOWN
    failure_class: ObservedFailureClass # Observed classification
