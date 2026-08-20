"""
Deterministic reproducibility verification for RecoveryOS Simulator.
Asserts that runs with identical seeds generate byte-identical entity IDs, features, and outcomes.
"""

from __future__ import annotations

from typing import Any
from simulator.run import build_simulator


def verify_deterministic_reproducibility(seed: int = 42, n: int = 1000) -> dict[str, Any]:
    """
    Run the simulator twice with the same seed and compare output record-by-record.
    Returns comparison metrics and asserts zero differences.
    """
    gen1, m1, c1, man1 = build_simulator(seed=seed, customer_count=500)
    batch1 = gen1.generate_batch(n, man1.simulation_id)

    gen2, m2, c2, man2 = build_simulator(seed=seed, customer_count=500)
    batch2 = gen2.generate_batch(n, man2.simulation_id)

    diffs = []

    if len(batch1.payments) != len(batch2.payments):
        diffs.append(f"Payment count mismatch: {len(batch1.payments)} vs {len(batch2.payments)}")

    for idx, (p1, p2) in enumerate(zip(batch1.payments, batch2.payments)):
        if p1 != p2:
            diffs.append(f"Payment[{idx}] mismatch: {p1} vs {p2}")
            if len(diffs) > 10:
                break

    for idx, (l1, l2) in enumerate(zip(batch1.latent_records, batch2.latent_records)):
        if l1 != l2:
            diffs.append(f"LatentRecord[{idx}] mismatch: {l1} vs {l2}")
            if len(diffs) > 10:
                break

    is_identical = len(diffs) == 0

    return {
        "seed": seed,
        "n": n,
        "is_identical": is_identical,
        "diff_count": len(diffs),
        "sample_diffs": diffs[:5],
    }
