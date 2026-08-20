"""
Isolated pseudo-random number generator streams for RecoveryOS Simulator.
Separates RNG states across domains (merchants, customers, payments, scenarios, latent state)
so that modifying generation logic in one subsystem doesn't perturb the randomness of others.
"""

from __future__ import annotations

import random
from typing import Sequence, TypeVar

T = TypeVar("T")


class SimRng:
    """
    Subsystem-isolated random number streams derived deterministically from a master seed.
    """

    def __init__(self, master_seed: int):
        self.master_seed = master_seed
        # Initialize isolated RNG instances
        self.merchants = random.Random(master_seed ^ 0x1A2B3C4D)
        self.customers = random.Random(master_seed ^ 0x5E6F7A8B)
        self.payments = random.Random(master_seed ^ 0x9C0D1E2F)
        self.scenarios = random.Random(master_seed ^ 0x3A4B5C6D)
        self.noise = random.Random(master_seed ^ 0x7E8F9A0B)
        self.latent = random.Random(master_seed ^ 0xB1C2D3E4)

    def choice(self, stream: str, seq: Sequence[T]) -> T:
        rng = getattr(self, stream)
        return rng.choice(seq)

    def choices(
        self, stream: str, population: Sequence[T], weights: Sequence[float] | None = None, k: int = 1
    ) -> list[T]:
        rng = getattr(self, stream)
        return rng.choices(population, weights=weights, k=k)

    def uniform(self, stream: str, a: float = 0.0, b: float = 1.0) -> float:
        rng = getattr(self, stream)
        return rng.uniform(a, b)

    def gauss(self, stream: str, mu: float = 0.0, sigma: float = 1.0) -> float:
        rng = getattr(self, stream)
        return rng.gauss(mu, sigma)

    def lognormvariate(self, stream: str, mu: float, sigma: float) -> float:
        rng = getattr(self, stream)
        return rng.lognormvariate(mu, sigma)

    def randint(self, stream: str, a: int, b: int) -> int:
        rng = getattr(self, stream)
        return rng.randint(a, b)
