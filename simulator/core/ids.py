"""
Deterministic ID generation for RecoveryOS Simulator.
Uses UUIDv5 derived from simulation seed, entity type, and sequence index
to guarantee 100% byte-identical entity IDs across deterministic runs.
"""

from __future__ import annotations

import uuid

# Fixed namespace UUID for RecoveryOS Simulator
SIMULATOR_NAMESPACE = uuid.UUID("7a3e9c12-5b8f-4d92-a16e-8c3b5d2e7f91")


class DeterministicIdGenerator:
    """
    Generates repeatable UUID strings based on a simulation seed, entity type, and index.
    """

    def __init__(self, seed: int):
        self.seed = seed

    def get_id(self, entity_type: str, index: int) -> str:
        """
        Generate a deterministic UUID string for a given entity type and index.
        """
        name = f"sim:{self.seed}:{entity_type}:{index}"
        return str(uuid.uuid5(SIMULATOR_NAMESPACE, name))

    def merchant_id(self, index: int) -> str:
        return self.get_id("merchant", index)

    def customer_id(self, index: int) -> str:
        return self.get_id("customer", index)

    def payment_id(self, index: int) -> str:
        return self.get_id("payment", index)

    def event_id(self, index: int) -> str:
        return self.get_id("event", index)

    def latent_id(self, index: int) -> str:
        return self.get_id("latent", index)

    def simulation_id(self) -> str:
        return self.get_id("manifest", 0)
