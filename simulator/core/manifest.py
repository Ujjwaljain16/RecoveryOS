"""
Simulation manifest definition for tracking and reproducing simulation runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class SimulationManifestData:
    simulation_id: str
    seed: int
    generator_version: str
    scenario_config: dict[str, Any]
    latent_function_version: str
    total_payments: int
    created_at: datetime
