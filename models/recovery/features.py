"""
FeatureTransformer: leakage-safe feature engineering for the recovery propensity model.

CRITICAL INVARIANT:
    - fit() is called on TRAIN split only.
    - The fitted transformer is pickled to artifacts/.
    - transform() uses the frozen fitted transformer — never re-fits on val/test.
    - Violation is tested in tests/unit/test_episodes.py::TestFeatureTransformer.

Encoding strategy:
    - Categoricals (bank, method, failure_class, failure_code, merchant_id):
        → OneHotEncoder for LogisticRegression and MLP
        → Passed as-is (string) for LightGBM (native categorical handling)
    - Continuous (amount_paise): log1p transform, then StandardScaler
    - Cyclic (hour_of_day, day_of_week): sin/cos encoding
    - Binary: passed as-is (0/1 int)
    - Ordinal (customer_ltv_decile): [1..10], genuinely ordinal — kept as int
"""

from __future__ import annotations

import math
import pickle
from pathlib import Path
from typing import Any

import numpy as np

try:
    import pandas as pd
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
TRANSFORMER_PATH = ARTIFACTS_DIR / "feature_transformer_v1.pkl"

# Categorical columns — OneHot for LR/MLP
CATEGORICAL_COLS = [
    "method",
    "bank",
    "initial_failure_class",
    "initial_failure_code",
    "merchant_id",
]
# Continuous columns
CONTINUOUS_COLS = ["amount_paise"]
# Cyclic columns
CYCLIC_COLS = {"hour_of_day": 24, "day_of_week": 7}
# Pass-through cols (already numeric, no transformation needed)
PASSTHROUGH_COLS = ["is_returning_customer", "customer_ltv_decile"]


class FeatureTransformer:
    """
    Fit on train only. Transform val/test using frozen state.

    Attributes:
        _fitted: bool — whether fit() has been called
        _ohe: OneHotEncoder (fitted on train categorical columns)
        _scaler: StandardScaler (fitted on train continuous columns)
        _train_payment_ids_hash: int — hash of train episode_ids for audit
    """

    def __init__(self) -> None:
        if not HAS_SKLEARN:
            raise ImportError("scikit-learn required: pip install scikit-learn")
        self._fitted = False
        self._ohe = OneHotEncoder(handle_unknown="ignore", sparse_output=False)
        self._scaler = StandardScaler()
        self._train_episode_ids_hash: int | None = None

    def fit(
        self, features_df: "pd.DataFrame", episode_ids: list[str] | None = None
    ) -> "FeatureTransformer":
        """
        Fit transformer on TRAIN features only.
        episode_ids should be the train episode_id list for audit purposes.
        """
        present_cats = [c for c in CATEGORICAL_COLS if c in features_df.columns]
        self._ohe.fit(features_df[present_cats].fillna("__MISSING__").astype(str))
        self._present_cats = present_cats

        present_cont = [c for c in CONTINUOUS_COLS if c in features_df.columns]
        self._present_cont = present_cont
        if present_cont:
            log_amounts = np.log1p(features_df[present_cont].values.astype(float))
            self._scaler.fit(log_amounts)

        if episode_ids is not None:
            # Store hash of train episode IDs for leakage audit
            self._train_episode_ids_hash = hash(tuple(sorted(episode_ids)))

        self._fitted = True
        return self

    def transform(self, features_df: "pd.DataFrame") -> np.ndarray:
        """
        Apply frozen transformer to a features dataframe.
        Raises RuntimeError if called before fit().
        """
        if not self._fitted:
            raise RuntimeError(
                "FeatureTransformer.transform() called before fit(). Call fit() on train data first."
            )

        parts: list[np.ndarray] = []

        # 1. One-hot encoded categoricals
        if self._present_cats:
            cat_data = features_df[self._present_cats].fillna("__MISSING__").astype(str)
            ohe_result = self._ohe.transform(cat_data)
            parts.append(ohe_result)

        # 2. Log-scaled + standardized continuous
        if self._present_cont:
            cont_data = features_df[self._present_cont].values.astype(float)
            log_cont = np.log1p(cont_data)
            parts.append(self._scaler.transform(log_cont))

        # 3. Cyclic encoding for hour_of_day and day_of_week
        for col, period in CYCLIC_COLS.items():
            if col in features_df.columns:
                vals = features_df[col].values.astype(float)
                parts.append(np.sin(2 * math.pi * vals / period).reshape(-1, 1))
                parts.append(np.cos(2 * math.pi * vals / period).reshape(-1, 1))

        # 4. Pass-through (binary + ordinal)
        for col in PASSTHROUGH_COLS:
            if col in features_df.columns:
                parts.append(features_df[col].values.reshape(-1, 1).astype(float))

        return np.hstack(parts)

    def fit_transform(
        self, features_df: "pd.DataFrame", episode_ids: list[str] | None = None
    ) -> np.ndarray:
        """Convenience: fit then transform in one call (use ONLY on train data)."""
        return self.fit(features_df, episode_ids).transform(features_df)

    def get_feature_names(self) -> list[str]:
        """Return human-readable feature names for SHAP/importance analysis."""
        names: list[str] = []
        if self._fitted and self._present_cats:
            names.extend(self._ohe.get_feature_names_out(self._present_cats).tolist())
        if self._fitted and self._present_cont:
            names.extend([f"log_{c}" for c in self._present_cont])
        for col, _ in CYCLIC_COLS.items():
            names.extend([f"{col}_sin", f"{col}_cos"])
        for col in PASSTHROUGH_COLS:
            names.append(col)
        return names

    def save(self, path: Path | None = None) -> Path:
        """Persist fitted transformer to disk."""
        if not self._fitted:
            raise RuntimeError("Cannot save unfitted transformer.")
        out_path = path or TRANSFORMER_PATH
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            pickle.dump(self, f)
        print(f"[FeatureTransformer] Saved to {out_path}")
        return out_path

    @classmethod
    def load(cls, path: Path | None = None) -> "FeatureTransformer":
        """Load a previously fitted transformer."""
        load_path = path or TRANSFORMER_PATH
        with open(load_path, "rb") as f:
            obj = pickle.load(f)
        if not isinstance(obj, cls):
            raise TypeError(f"Expected FeatureTransformer, got {type(obj)}")
        return obj
