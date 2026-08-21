"""
Latent Recoverability Function for RecoveryOS Simulator (TRD §6).
Computes time-dependent, non-linear true recovery probability and ground_truth_recoverable
using unobserved latent variables (customer patience decay, latent bank health, latent noise).
"""

from __future__ import annotations

import math
from datetime import datetime

from simulator.core.rng import SimRng
from simulator.customers.generator import SimulatedCustomer
from simulator.failures.codes import TrueFailureType
from simulator.outcomes.latent_state import LatentStateRecord


def _sigmoid(x: float) -> float:
    # Stable sigmoid
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    else:
        z = math.exp(x)
        return z / (1.0 + z)


class LatentRecoverabilityFunction:
    """
    Computes true ground-truth recoverability.
    
    CRITICAL NON-CIRCULARITY INVARIANT:
    The ground-truth outcome is determined by latent unobserved dynamics:
      1. Customer patience decay: decays exponentially with attempts and latency.
      2. True bank health: underlying rail capacity/recovery curve.
      3. Unobserved customer propensity: latent financial liquidity / app loyalty.
      4. Latent stochastic noise: unobservable real-world externalities.
    
    This function is NEVER accessible to the ML model or AI diagnoser at inference time.
    """

    VERSION = "latent-v2.0"

    def __init__(self, rng: SimRng):
        self.rng = rng

    def compute_latent_recovery(
        self,
        simulation_id: str,
        latent_id: str,
        payment_id: str,
        customer: SimulatedCustomer,
        true_failure_type: TrueFailureType,
        latent_bank_health: float,
        attempt_number: int,
        timestamp: datetime,
    ) -> tuple[bool, LatentStateRecord]:
        """
        Compute true recovery probability and binary ground-truth recoverability.
        Returns:
            (ground_truth_recoverable: bool, latent_record: LatentStateRecord)
        """
        if true_failure_type == TrueFailureType.SUCCESS:
            # Payment already succeeded — not a failed recovery candidate
            latent_record = LatentStateRecord(
                latent_id=latent_id,
                simulation_id=simulation_id,
                payment_id=payment_id,
                customer_patience_score=1.0,
                bank_latent_health=1.0,
                latent_network_noise=0.0,
                latent_customer_propensity=0.0,
                true_recovery_prob_bps=10000,
                true_failure_type=true_failure_type.value,
                created_at=timestamp,
            )
            return True, latent_record

        # 1. Latent Customer Patience Decay: P_cust(attempt) = P_0 * exp(-lambda * (attempt - 1))
        decay_rate = 0.45
        customer_patience = customer.latent_patience_mean * math.exp(-decay_rate * (attempt_number - 1))
        customer_patience = max(0.01, min(1.0, customer_patience))

        # 2. Latent Bank Health
        bank_health = max(0.01, min(1.0, latent_bank_health))

        # 3. Latent Customer Propensity
        customer_propensity = customer.latent_propensity_bias

        # 4. Latent External Stochastic Noise: N(0, 1.5) to ensure AUC < 0.85 leakage threshold
        latent_noise = self.rng.gauss("latent", mu=0.0, sigma=1.5)

        # 5. True Failure Root Factor:
        # Permanent errors have extremely negative base affinity (essentially unrecoverable)
        if true_failure_type in (
            TrueFailureType.PERMANENT_INVALID_CREDS,
            TrueFailureType.PERMANENT_EXPIRED_INSTRUMENT,
            TrueFailureType.PERMANENT_ACCOUNT_CLOSED,
        ):
            root_affinity = -3.8
        elif true_failure_type == TrueFailureType.CUSTOMER_INSUFFICIENT_FUNDS:
            # Low recoverability unless customer patience/propensity is high
            root_affinity = -1.2
        elif true_failure_type in (
            TrueFailureType.BANK_DEGRADATION_FAIL,
            TrueFailureType.MULTI_RAIL_OUTAGE_FAIL,
        ):
            # Recovery heavily hinges on bank_health healing over time
            root_affinity = -0.3 + 1.8 * (bank_health - 0.5)
        elif true_failure_type == TrueFailureType.TEMPORARY_GATEWAY_TIMEOUT:
            # High natural recoverability if retried
            root_affinity = 0.8
        elif true_failure_type == TrueFailureType.TRANSIENT_NETWORK_DROP:
            root_affinity = 1.2
        else:
            root_affinity = 0.0

        # Latent Logit Model:
        # logit = α + β1 * patience + β2 * bank_health + β3 * propensity + β4 * noise + root_affinity
        alpha = -0.2
        logit = (
            alpha
            + 1.6 * (customer_patience - 0.5)
            + 1.4 * (bank_health - 0.5)
            + 1.2 * customer_propensity
            + latent_noise
            + root_affinity
        )

        true_prob = _sigmoid(logit)
        # Convert to integer basis points [0, 10000]
        true_prob_bps = max(0, min(10000, int(round(true_prob * 10000))))

        # Sample binary ground truth outcome using the latent RNG stream
        is_recoverable = self.rng.uniform("latent") < true_prob

        latent_record = LatentStateRecord(
            latent_id=latent_id,
            simulation_id=simulation_id,
            payment_id=payment_id,
            customer_patience_score=round(customer_patience, 4),
            bank_latent_health=round(bank_health, 4),
            latent_network_noise=round(latent_noise, 4),
            latent_customer_propensity=round(customer_propensity, 4),
            true_recovery_prob_bps=true_prob_bps,
            true_failure_type=true_failure_type.value,
            created_at=timestamp,
        )

        return is_recoverable, latent_record
