"""
Realistic distributions for payment amount, method mix, and banking rails (PRD §31).
"""

from __future__ import annotations

import math
from typing import Sequence

from simulator.core.rng import SimRng
from simulator.merchants.models import SimulatedMerchant

# 5 Supported Payment Methods & empirical shares in India (PRD §31)
PAYMENT_METHODS = ["upi", "card", "netbanking", "wallet"]
METHOD_WEIGHTS = [0.55, 0.25, 0.15, 0.05]

# Major Indian banking institutions
INDIAN_BANKS = ["HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "YESB"]
BANK_WEIGHTS = [0.28, 0.22, 0.20, 0.15, 0.10, 0.05]


class PaymentDistributionSampler:
    """
    Samples payment amounts, methods, and bank rails with log-normal amounts in integer paise.
    """

    def __init__(self, rng: SimRng):
        self.rng = rng

    def sample_method(self) -> str:
        """Sample a payment method according to Indian market distribution."""
        return self.rng.choices("payments", PAYMENT_METHODS, weights=METHOD_WEIGHTS, k=1)[0]

    def sample_bank(self) -> str:
        """Sample a bank rail according to transaction volume distribution."""
        return self.rng.choices("payments", INDIAN_BANKS, weights=BANK_WEIGHTS, k=1)[0]

    def sample_amount_paise(self, merchant: SimulatedMerchant, method: str) -> int:
        """
        Sample an amount in paise from a log-normal distribution centered around
        the merchant's typical ticket size, modulated by payment method.
        All monetary math strictly uses integers (paise).
        """
        target_avg = merchant.avg_ticket_paise
        # Method-specific ticket modulation (cards & netbanking higher, UPI lower)
        if method == "upi":
            mu_adj = 0.8
            sigma = 0.6
        elif method == "card":
            mu_adj = 1.3
            sigma = 0.7
        elif method == "netbanking":
            mu_adj = 1.8
            sigma = 0.8
        else:  # wallet
            mu_adj = 0.5
            sigma = 0.5

        mean_paise = max(5000, target_avg * mu_adj)
        # Convert to log-normal parameters
        # mu = ln(mean) - 0.5 * sigma^2
        mu = math.log(mean_paise) - 0.5 * (sigma**2)
        raw_val = self.rng.lognormvariate("payments", mu=mu, sigma=sigma)

        # Clamp between ₹10 (1,000 paise) and ₹200,000 (20,000,000 paise)
        amount_paise = max(1000, min(20_000_000, int(raw_val)))
        return amount_paise
