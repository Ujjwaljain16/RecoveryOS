"""
Protocol and interfaces for composable failure scenario modifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from simulator.customers.generator import SimulatedCustomer
from simulator.failures.codes import TrueFailureType
from simulator.merchants.models import SimulatedMerchant


@dataclass(frozen=True)
class PaymentContext:
    """Contextual features available when evaluating scenario applicability."""
    payment_id: str
    merchant: SimulatedMerchant
    customer: SimulatedCustomer
    amount_paise: int
    method: str
    bank: str
    attempt_number: int
    timestamp: datetime


@runtime_checkable
class ScenarioModifier(Protocol):
    """
    Protocol for composable failure scenarios.
    Scenarios can layer on top of each other, modifying baseline failure probabilities,
    true failure states, and latent recovery dynamics.
    """

    name: str

    def applies_to(self, context: PaymentContext) -> bool:
        """Determine if this scenario modifier is active for the given payment context."""
        ...

    def get_failure_rate(self, context: PaymentContext) -> float:
        """Get the specific failure rate contribution of this scenario for the context."""
        ...

    def modify_failure_probability(self, base_prob: float, context: PaymentContext) -> float:
        """Modify the probability that this payment attempt fails."""
        ...

    def determine_true_failure_type(self, context: PaymentContext) -> TrueFailureType:
        """Return the true underlying root failure type if this scenario is the primary cause."""
        ...

    def modify_latent_health(self, base_health: float, context: PaymentContext) -> float:
        """Modify unobserved latent bank/rail health factor."""
        ...

