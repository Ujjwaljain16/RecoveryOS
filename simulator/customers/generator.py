"""
Customer entity generator for RecoveryOS Simulator.
Generates 2,000 customers distributed across merchants with Pareto lifetime value,
returning vs. new status mix, and baseline opt-out probability (PRD §31, gaps.md §A.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from simulator.core.ids import DeterministicIdGenerator
from simulator.core.rng import SimRng
from simulator.merchants.models import SimulatedMerchant


@dataclass(frozen=True)
class SimulatedCustomer:
    customer_id: str
    merchant_id: str
    is_returning: bool
    lifetime_value_paise: int
    opted_out_at: datetime | None
    created_at: datetime
    # Latent customer characteristics (used solely by latent ground-truth function)
    latent_patience_mean: float       # baseline patience [0.1, 0.9]
    latent_propensity_bias: float     # inherent payment reliability [-0.5, 0.5]


class CustomerGenerator:
    """
    Generates 2,000 simulated customers per PRD §31.
    """

    def __init__(
        self,
        id_gen: DeterministicIdGenerator,
        rng: SimRng,
        base_time: datetime,
        opt_out_baseline_rate: float = 0.04,
    ):
        self.id_gen = id_gen
        self.rng = rng
        self.base_time = base_time
        self.opt_out_baseline_rate = opt_out_baseline_rate

    def generate_customers(
        self, count: int, merchants: Sequence[SimulatedMerchant]
    ) -> list[SimulatedCustomer]:
        customers: list[SimulatedCustomer] = []
        for idx in range(count):
            c_id = self.id_gen.customer_id(idx)
            # Distribute across merchants
            merchant = self.rng.choice("customers", merchants)

            # 60% returning, 40% new
            is_returning = self.rng.uniform("customers") < 0.60

            # LTV Pareto distribution in paise:
            # New customers: lower LTV (₹500 to ₹5,000)
            # Returning customers: Pareto/log-normal tail (₹5,000 to ₹500,000)
            if is_returning:
                # Pareto alpha=1.8, scaled to paise
                ltv_raw = (self.rng.customers.paretovariate(1.8) - 1.0) * 250_000 + 50_000
                ltv_paise = max(50_000, min(50_000_000, int(ltv_raw)))
            else:
                ltv_paise = self.rng.randint("customers", 0, 50_000)

            # Baseline opt-out probability (4%)
            is_opted_out = self.rng.uniform("customers") < self.opt_out_baseline_rate
            opted_out_at = (
                self.base_time - timedelta(days=self.rng.randint("customers", 1, 30))
                if is_opted_out
                else None
            )

            # Customer creation timestamp (returning customers created earlier)
            if is_returning:
                created_at = self.base_time - timedelta(days=self.rng.randint("customers", 10, 180))
            else:
                created_at = self.base_time - timedelta(days=self.rng.randint("customers", 0, 5))

            # Latent unobserved traits
            latent_patience = max(0.05, min(0.95, self.rng.gauss("latent", mu=0.5, sigma=0.15)))
            latent_propensity = max(-0.5, min(0.5, self.rng.gauss("latent", mu=0.0, sigma=0.2)))

            customers.append(
                SimulatedCustomer(
                    customer_id=c_id,
                    merchant_id=merchant.merchant_id,
                    is_returning=is_returning,
                    lifetime_value_paise=ltv_paise,
                    opted_out_at=opted_out_at,
                    created_at=created_at,
                    latent_patience_mean=latent_patience,
                    latent_propensity_bias=latent_propensity,
                )
            )

        return customers
