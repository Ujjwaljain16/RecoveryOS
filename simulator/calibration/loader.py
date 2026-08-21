"""
Calibration parameter loader for RecoveryOS Simulator.
Loads and validates simulator/calibration/parameters.yaml.
Exposes a typed CalibrationParameters dataclass.

All distribution constants in the simulator should be sourced from here,
not hardcoded as Python magic numbers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml


PARAMETERS_YAML_PATH = Path(__file__).parent / "parameters.yaml"


@dataclass(frozen=True)
class CalibrationParameter:
    name: str
    value: float
    source: str
    source_period: str
    metric_definition: str
    unit: str
    transformation: str
    confidence: str
    source_url: str | None = None
    applies_to: str | None = None


@dataclass(frozen=True)
class CalibrationParameters:
    """Typed access to all calibration constants used by the simulator."""

    # Method distribution
    upi_transaction_share: float
    card_transaction_share: float
    netbanking_transaction_share: float
    wallet_transaction_share: float

    # Amount distribution
    upi_amount_median_paise: int
    amount_lognormal_sigma: float

    # Failure rates
    baseline_failure_rate: float

    # Retry economics
    fixed_retry_cost_paise: int
    variable_retry_cost_rate: float
    recovery_margin: float

    # Time-of-day
    peak_hour_failure_multiplier: float

    @property
    def method_weights(self) -> dict[str, float]:
        return {
            "upi": self.upi_transaction_share,
            "card": self.card_transaction_share,
            "netbanking": self.netbanking_transaction_share,
            "wallet": self.wallet_transaction_share,
        }


@lru_cache(maxsize=1)
def load_calibration(path: str | None = None) -> CalibrationParameters:
    """
    Load and validate calibration parameters from YAML.
    Cached — called once at module import time.
    Raises ValueError if any required metadata field is missing.
    """
    yaml_path = Path(path) if path else PARAMETERS_YAML_PATH
    with open(yaml_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    required_fields = {
        "name", "value", "source", "source_period",
        "metric_definition", "unit", "transformation", "confidence",
    }
    params: dict[str, CalibrationParameter] = {}

    for entry in raw["parameters"]:
        missing = required_fields - set(entry.keys())
        if missing:
            raise ValueError(
                f"Calibration parameter '{entry.get('name', '?')}' "
                f"is missing required metadata fields: {missing}"
            )
        cp = CalibrationParameter(
            name=entry["name"],
            value=float(entry["value"]),
            source=str(entry["source"]),
            source_period=str(entry["source_period"]),
            metric_definition=str(entry["metric_definition"]).strip(),
            unit=str(entry["unit"]),
            transformation=str(entry["transformation"]).strip(),
            confidence=str(entry["confidence"]),
            source_url=entry.get("source_url"),
            applies_to=entry.get("applies_to"),
        )
        params[cp.name] = cp

    def get(name: str) -> float:
        if name not in params:
            raise KeyError(f"Calibration parameter '{name}' not found in parameters.yaml")
        return params[name].value

    return CalibrationParameters(
        upi_transaction_share=get("upi_transaction_share"),
        card_transaction_share=get("card_transaction_share"),
        netbanking_transaction_share=get("netbanking_transaction_share"),
        wallet_transaction_share=get("wallet_transaction_share"),
        upi_amount_median_paise=int(get("upi_amount_median_paise")),
        amount_lognormal_sigma=get("amount_lognormal_sigma"),
        baseline_failure_rate=get("baseline_failure_rate"),
        fixed_retry_cost_paise=int(get("fixed_retry_cost_paise")),
        variable_retry_cost_rate=get("variable_retry_cost_rate"),
        recovery_margin=get("recovery_margin"),
        peak_hour_failure_multiplier=get("peak_hour_failure_multiplier"),
    )
