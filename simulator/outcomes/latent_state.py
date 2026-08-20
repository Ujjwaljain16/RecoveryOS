"""
Latent state domain definitions for RecoveryOS Simulator.
These values represent unobserved reality and are NEVER exposed to the inference pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class LatentStateRecord:
    latent_id: str
    simulation_id: str
    payment_id: str
    customer_patience_score: float      # Unobserved patience decay [0.0, 1.0]
    bank_latent_health: float           # True underlying bank infrastructure health [0.0, 1.0]
    latent_network_noise: float         # Non-linear unobserved stochastic jitter [-1.0, 1.0]
    latent_customer_propensity: float   # Unobserved user willingness/ability to complete payment [-1.0, 1.0]
    true_recovery_prob_bps: int         # True latent recovery probability in basis points (0-10000)
    true_failure_type: str              # True underlying failure state
    created_at: datetime
