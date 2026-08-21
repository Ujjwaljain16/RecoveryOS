"""simulator.dataset — structured episode dataset building."""
from simulator.dataset.schema import (
    VISIBLE_FEATURE_COLUMNS,
    LABEL_COLUMNS,
    PROHIBITED_IN_FEATURES,
    assert_no_leakage,
)
from simulator.dataset.builder import DatasetBuilder, DatasetManifest, SplitManifest

__all__ = [
    "VISIBLE_FEATURE_COLUMNS",
    "LABEL_COLUMNS",
    "PROHIBITED_IN_FEATURES",
    "assert_no_leakage",
    "DatasetBuilder",
    "DatasetManifest",
    "SplitManifest",
]
