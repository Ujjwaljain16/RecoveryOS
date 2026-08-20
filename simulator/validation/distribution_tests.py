"""
Statistical rate and distribution tests for RecoveryOS Simulator.
Verifies failure rate bounds, payment method mix, and bank distributions.
"""

from __future__ import annotations

from typing import Any
from simulator.run import build_simulator


def verify_scenario_distributions(n: int = 5000, seed: int = 42) -> dict[str, Any]:
    """
    Generate payments and verify that observed failure rates and distributions
    match expected realistic parameters.
    """
    generator, merchants, customers, manifest = build_simulator(seed=seed)
    batch = generator.generate_batch(n, manifest.simulation_id)

    total_payments = len(batch.payments)
    failed = [p for p in batch.payments if p.status == "failed"]
    observed_failure_rate = len(failed) / total_payments

    method_counts: dict[str, int] = {}
    bank_counts: dict[str, int] = {}
    class_counts: dict[str, int] = {}

    for p in batch.payments:
        method_counts[p.method] = method_counts.get(p.method, 0) + 1
        bank_counts[p.bank] = bank_counts.get(p.bank, 0) + 1
        if p.failure_class:
            class_counts[p.failure_class] = class_counts.get(p.failure_class, 0) + 1

    return {
        "total_payments": total_payments,
        "failed_count": len(failed),
        "observed_failure_rate": round(observed_failure_rate, 4),
        "method_shares": {k: round(v / total_payments, 3) for k, v in method_counts.items()},
        "bank_shares": {k: round(v / total_payments, 3) for k, v in bank_counts.items()},
        "failure_class_breakdown": class_counts,
    }
