"""
Failures and scenarios subsystem.
"""

from simulator.failures.codes import (
    ObservedFailure,
    ObservedFailureClass,
    TrueFailureType,
)
from simulator.failures.observation_noise import ObservationNoisePipeline
from simulator.failures.protocol import PaymentContext, ScenarioModifier
from simulator.failures.scenarios import (
    BankDegradationScenario,
    CustomerRepeatFailureScenario,
    MultiRailOutageScenario,
    NormalFailureScenario,
    PermanentFailureScenario,
    TemporaryTimeoutScenario,
)

__all__ = [
    "TrueFailureType",
    "ObservedFailureClass",
    "ObservedFailure",
    "PaymentContext",
    "ScenarioModifier",
    "ObservationNoisePipeline",
    "NormalFailureScenario",
    "BankDegradationScenario",
    "MultiRailOutageScenario",
    "TemporaryTimeoutScenario",
    "PermanentFailureScenario",
    "CustomerRepeatFailureScenario",
]
