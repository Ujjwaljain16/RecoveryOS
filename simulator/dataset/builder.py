"""
DatasetBuilder: generates train/val/test splits from episode batches.

Layout:
    data/
    ├── train/
    │   ├── features.parquet   ← visible features only (VISIBLE_FEATURE_COLUMNS)
    │   └── labels.parquet     ← actual_recovered, optimal_recovery_action
    ├── val_random/            ← decorrelated seed (train_seed + offset), random sample
    ├── val_temporal/          ← same seed as train, later clock timestamps
    ├── test_random/           ← seed=999
    ├── test_temporal/         ← same seed as test_random, later clock timestamps
    ├── test_scenario/         ← decorrelated seed (test_seed + offset), OOD scenario weights
    └── manifest.json          ← per-split seed, sizes, schema version, created_at

Leakage invariant: features.parquet is asserted free of latent/label columns
before writing. Tested in tests/unit/test_dataset_schema.py.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from simulator.dataset.schema import (
    LABEL_COLUMNS,
    VISIBLE_FEATURE_COLUMNS,
    assert_no_leakage,
)
from simulator.episodes.models import RecoveryEpisode

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False


DATA_ROOT = Path("data")
SCHEMA_VERSION = "1.0"

# Temporal split boundary: episodes with clock_timestamp.day <= TRAIN_CUTOFF_DAY go to train
# (within the same month). Adjust as needed.
TRAIN_CUTOFF_DAY = 21   # Jan 1–21 → train
VAL_TEMPORAL_CUTOFF_DAY = 25  # Jan 22–25 → val_temporal
# Jan 26+ → test_temporal


@dataclass
class SplitManifest:
    split_name: str
    seed: int
    n_episodes: int
    n_recovered: int
    n_retry_now_optimal: int
    recovery_rate: float
    retry_now_rate: float


@dataclass
class DatasetManifest:
    schema_version: str
    created_at: str
    splits: list[SplitManifest]
    train_seed: int
    test_seed: int
    feature_columns: list[str]
    label_columns: list[str]


def _episode_to_feature_row(ep: RecoveryEpisode) -> dict[str, Any]:
    """Extract visible features from an episode. No latent fields."""
    return {
        "episode_id": ep.episode_id,
        "amount_paise": ep.amount_paise,
        "method": ep.method,
        "bank": ep.bank,
        "is_returning_customer": int(ep.is_returning_customer),
        "customer_ltv_decile": ep.customer_ltv_decile,
        "initial_failure_code": ep.initial_failure_code,
        "initial_failure_class": ep.initial_failure_class,
        "hour_of_day": ep.hour_of_day,
        "day_of_week": ep.day_of_week,
        "merchant_id": ep.merchant_id,
    }


def _episode_to_label_row(ep: RecoveryEpisode) -> dict[str, Any]:
    """Extract ground-truth labels from an episode."""
    return {
        "episode_id": ep.episode_id,
        "actual_recovered": int(ep.actual_recovered),
        "optimal_recovery_action": ep.optimal_recovery_action,
    }


def _split_temporal(
    episodes: list[RecoveryEpisode],
) -> tuple[list[RecoveryEpisode], list[RecoveryEpisode]]:
    """Split episodes into (early, late) by clock_timestamp day."""
    early = [e for e in episodes if e.clock_timestamp.day <= TRAIN_CUTOFF_DAY]
    late = [e for e in episodes if e.clock_timestamp.day > TRAIN_CUTOFF_DAY]
    return early, late


def write_split(
    episodes: list[RecoveryEpisode],
    split_name: str,
    output_dir: Path,
) -> SplitManifest:
    """
    Write features.parquet and labels.parquet for a split.
    Enforces leakage assertion before writing.
    """
    if not HAS_PANDAS:
        raise ImportError("pandas is required for dataset building: pip install pandas pyarrow")

    split_dir = output_dir / split_name
    split_dir.mkdir(parents=True, exist_ok=True)

    feature_rows = [_episode_to_feature_row(ep) for ep in episodes]
    label_rows = [_episode_to_label_row(ep) for ep in episodes]

    features_df = pd.DataFrame(feature_rows)
    labels_df = pd.DataFrame(label_rows)

    # ── Leakage assertion: fail loudly before touching disk ──────────────────
    assert_no_leakage(list(features_df.columns), context=f"split={split_name} features.parquet")

    features_df.to_parquet(split_dir / "features.parquet", index=False)
    labels_df.to_parquet(split_dir / "labels.parquet", index=False)

    n_recovered = int(labels_df["actual_recovered"].sum())
    n_retry_now = int((labels_df["optimal_recovery_action"] == "RETRY_NOW").sum())
    n = len(episodes)

    print(f"  [{split_name}] {n:,} episodes | recovery_rate={n_recovered/n:.3f} | retry_now={n_retry_now/n:.3f}")

    return SplitManifest(
        split_name=split_name,
        seed=-1,  # filled by caller
        n_episodes=n,
        n_recovered=n_recovered,
        n_retry_now_optimal=n_retry_now,
        recovery_rate=round(n_recovered / n, 4),
        retry_now_rate=round(n_retry_now / n, 4),
    )


class DatasetBuilder:
    """
    Orchestrates episode generation and writes structured train/val/test splits.

    Usage:
        builder = DatasetBuilder(output_dir=Path("data"))
        builder.build(
            train_episodes=train_eps,
            val_episodes=val_eps,
            test_episodes=test_eps,
            train_seed=42,
            test_seed=999,
        )
    """

    def __init__(self, output_dir: Path = DATA_ROOT) -> None:
        self.output_dir = output_dir

    def build(
        self,
        train_episodes: list[RecoveryEpisode],
        val_episodes: list[RecoveryEpisode],
        test_episodes: list[RecoveryEpisode],
        test_scenario_episodes: list[RecoveryEpisode],
        train_seed: int,
        test_seed: int,
        val_seed: int | None = None,
        test_scenario_seed: int | None = None,
    ) -> DatasetManifest:
        print(f"[DatasetBuilder] Writing splits to {self.output_dir.resolve()}")
        split_manifests: list[SplitManifest] = []

        # Train: random (temporal-early) subset
        train_early, train_late = _split_temporal(train_episodes)
        m = write_split(train_early, "train", self.output_dir)
        m.seed = train_seed
        split_manifests.append(m)

        # Val temporal: later episodes from train seed run
        if train_late:
            m = write_split(train_late, "val_temporal", self.output_dir)
            m.seed = train_seed
            split_manifests.append(m)

        # Val random: explicitly provided val episodes. gaps.md sec:C.2 -- this
        # used to be generated with train_seed (a literal duplicate of train's
        # leading episodes); it now has its own decorrelated seed, recorded
        # here rather than mislabeled as train_seed.
        m = write_split(val_episodes, "val_random", self.output_dir)
        m.seed = val_seed if val_seed is not None else train_seed
        split_manifests.append(m)

        # Test random + temporal: from seed=999
        test_early, test_late = _split_temporal(test_episodes)
        m = write_split(test_early, "test_random", self.output_dir)
        m.seed = test_seed
        split_manifests.append(m)

        if test_late:
            m = write_split(test_late, "test_temporal", self.output_dir)
            m.seed = test_seed
            split_manifests.append(m)
            
        # Test scenario: out-of-distribution scenario params. gaps.md sec:C.2
        # -- this used to reuse test_seed (near-fully correlated with
        # test_random, same underlying draws); it now has its own
        # decorrelated seed, recorded here rather than mislabeled as test_seed.
        if test_scenario_episodes:
            m = write_split(test_scenario_episodes, "test_scenario", self.output_dir)
            m.seed = test_scenario_seed if test_scenario_seed is not None else test_seed
            split_manifests.append(m)

        manifest = DatasetManifest(
            schema_version=SCHEMA_VERSION,
            created_at=datetime.now(timezone.utc).isoformat(),
            splits=split_manifests,
            train_seed=train_seed,
            test_seed=test_seed,
            feature_columns=VISIBLE_FEATURE_COLUMNS,
            label_columns=LABEL_COLUMNS,
        )

        manifest_path = self.output_dir / "manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(
                {
                    "schema_version": manifest.schema_version,
                    "created_at": manifest.created_at,
                    "train_seed": manifest.train_seed,
                    "test_seed": manifest.test_seed,
                    "feature_columns": manifest.feature_columns,
                    "label_columns": manifest.label_columns,
                    "splits": [
                        {
                            "split_name": s.split_name,
                            "seed": s.seed,
                            "n_episodes": s.n_episodes,
                            "n_recovered": s.n_recovered,
                            "n_retry_now_optimal": s.n_retry_now_optimal,
                            "recovery_rate": s.recovery_rate,
                            "retry_now_rate": s.retry_now_rate,
                        }
                        for s in manifest.splits
                    ],
                },
                f,
                indent=2,
            )

        print(f"[DatasetBuilder] manifest.json written to {manifest_path}")
        return manifest
