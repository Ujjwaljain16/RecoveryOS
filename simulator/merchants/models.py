"""
Merchant entity representations and generators for RecoveryOS Simulator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from simulator.core.ids import DeterministicIdGenerator
from simulator.core.rng import SimRng


@dataclass(frozen=True)
class SimulatedMerchant:
    merchant_id: str
    name: str
    industry: str
    avg_ticket_paise: int
    created_at: datetime


class MerchantGenerator:
    """
    Generates 3 representative merchant profiles for Track 03 (PRD §31).
    """

    MERCHANT_PROFILES = [
        ("Acme Cloud SaaS", "B2B SaaS", 450_000),         # ₹4,500 avg ticket
        ("QuickBite Mart", "Quick Commerce", 65_000),      # ₹650 avg ticket
        ("StyleVerse Apparel", "D2C Fashion", 180_000),   # ₹1,800 avg ticket
    ]

    def __init__(self, id_gen: DeterministicIdGenerator, rng: SimRng, base_time: datetime):
        self.id_gen = id_gen
        self.rng = rng
        self.base_time = base_time

    def generate_merchants(self) -> list[SimulatedMerchant]:
        merchants: list[SimulatedMerchant] = []
        for idx, (name, industry, avg_ticket) in enumerate(self.MERCHANT_PROFILES):
            m_id = self.id_gen.merchant_id(idx)
            merchants.append(
                SimulatedMerchant(
                    merchant_id=m_id,
                    name=name,
                    industry=industry,
                    avg_ticket_paise=avg_ticket,
                    created_at=self.base_time,
                )
            )
        return merchants
