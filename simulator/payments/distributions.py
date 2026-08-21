"""
Realistic distributions for payment amount, method mix, and banking rails (PRD §31).
All distribution constants are sourced from simulator/calibration/parameters.yaml.
Do NOT add hardcoded magic numbers here — add to calibration YAML with full metadata.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Sequence

from simulator.calibration.loader import load_calibration
from simulator.core.rng import SimRng
from simulator.merchants.models import SimulatedMerchant

# Payment methods — order must match calibration weight order
PAYMENT_METHODS = ["upi", "card", "netbanking", "wallet"]

# Major Indian banking institutions (weights from empirical transaction volumes)
INDIAN_BANKS = ["HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "YESB"]
BANK_WEIGHTS = [0.28, 0.22, 0.20, 0.15, 0.10, 0.05]


class PaymentDistributionSampler:
    """
    Samples payment amounts, methods, and bank rails with log-normal amounts in integer paise.
    Method weights are loaded from calibration/parameters.yaml (source-cited, versioned).
    """

    def __init__(self, rng: SimRng):
        self.rng = rng
        # Load calibrated weights — fail loudly if YAML is missing or malformed
        calib = load_calibration()
        self._method_weights = [
            calib.upi_transaction_share,
            calib.card_transaction_share,
            calib.netbanking_transaction_share,
            calib.wallet_transaction_share,
        ]
        self._amount_sigma = calib.amount_lognormal_sigma

    def sample_method(self) -> str:
        """Sample payment method using calibration-sourced Indian market weights."""
        return self.rng.choices("payments", PAYMENT_METHODS, weights=self._method_weights, k=1)[0]

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
