"""
Recovery propensity model training script.

Trains three models on train split, evaluates on val_random and val_temporal.
Models:
    1. LogisticRegression (L2, Platt-calibrated) — linear baseline
    2. LightGBM (primary — native categoricals, SHAP importances)
    3. MLPClassifier (2 hidden layers [64, 32]) — non-linear baseline

Usage:
    python -m models.recovery.train --data-dir=data --output-dir=models/recovery/artifacts

Leakage gate runs as a pre-training check:
    LR AUC 95% CI upper bound < 0.85
    If violated: training stops. Fix the simulator, not the model.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

try:
    import pandas as pd
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.neural_network import MLPClassifier
    import lightgbm as lgb

    HAS_DEPS = True
except ImportError as e:
    HAS_DEPS = False
    _IMPORT_ERROR = str(e)

from models.recovery.features import FeatureTransformer, CATEGORICAL_COLS

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
LEAKAGE_AUC_THRESHOLD = 0.85
BOOTSTRAP_N = 1000
BOOTSTRAP_SEED = 12345


def _bootstrap_auc_ci(
    y_true: np.ndarray, y_score: np.ndarray, n: int = BOOTSTRAP_N
) -> tuple[float, float]:
    rng = np.random.RandomState(BOOTSTRAP_SEED)
    aucs = []
    for _ in range(n):
        idx = rng.choice(len(y_true), len(y_true), replace=True)
        yt, ys = y_true[idx], y_score[idx]
        if len(np.unique(yt)) < 2:
            continue
        aucs.append(roc_auc_score(yt, ys))
    aucs = np.array(aucs)
    return float(np.percentile(aucs, 2.5)), float(np.percentile(aucs, 97.5))


def _load_split(data_dir: Path, split: str) -> tuple["pd.DataFrame", "pd.DataFrame"]:
    features = pd.read_parquet(data_dir / split / "features.parquet")
    labels = pd.read_parquet(data_dir / split / "labels.parquet")
    return features, labels


def _run_leakage_gate(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
) -> dict:
    """
    Gate 1: Leakage check.
    Train a simple LR on visible features. If it achieves AUC >= 0.85,
    the visible features accidentally contain the answer. Stop training.
    """
    print("[LeakageGate] Training logistic regression on visible features...")
    lr = LogisticRegression(max_iter=500, C=1.0, random_state=42)
    lr.fit(X_train, y_train)
    y_proba = lr.predict_proba(X_val)[:, 1]
    auc = roc_auc_score(y_val, y_proba)
    lo, hi = _bootstrap_auc_ci(y_val, y_proba)

    result = {"lr_auc": round(auc, 4), "ci_lo": round(lo, 4), "ci_hi": round(hi, 4)}
    print(f"[LeakageGate] LR AUC = {auc:.4f} | 95% CI = [{lo:.4f}, {hi:.4f}]")

    if hi >= LEAKAGE_AUC_THRESHOLD:
        raise RuntimeError(
            f"LEAKAGE GATE FAILED: LR AUC 95% CI upper bound = {hi:.4f} >= {LEAKAGE_AUC_THRESHOLD}. "
            f"Visible features are too predictive. Investigate simulator ground-truth separation."
        )
    print(f"[LeakageGate] PASSED ✓ (CI upper = {hi:.4f} < {LEAKAGE_AUC_THRESHOLD})")
    return result


def train(data_dir: Path, output_dir: Path) -> None:
    if not HAS_DEPS:
        raise ImportError(
            f"Missing dependencies: {_IMPORT_ERROR}\npip install lightgbm scikit-learn pandas pyarrow"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # ── Load splits ────────────────────────────────────────────────────────────
    print("[Train] Loading train split...")
    feat_train, lab_train = _load_split(data_dir, "train")
    print("[Train] Loading val_random split...")
    feat_val, lab_val = _load_split(data_dir, "val_random")

    y_train = lab_train["actual_recovered"].values.astype(int)
    y_val = lab_val["actual_recovered"].values.astype(int)
    y_train_action = (lab_train["optimal_recovery_action"] == "RETRY_NOW").values.astype(int)
    y_val_action = (lab_val["optimal_recovery_action"] == "RETRY_NOW").values.astype(int)

    # ── Feature transformer (fit on train only) ───────────────────────────────
    print("[Train] Fitting FeatureTransformer on train split only...")
    transformer = FeatureTransformer()
    X_train = transformer.fit_transform(feat_train, episode_ids=feat_train["episode_id"].tolist())
    X_val = transformer.transform(feat_val)
    transformer.save(output_dir / "feature_transformer_v1.pkl")

    # ── Leakage gate (pre-training) ────────────────────────────────────────────
    leakage_result = _run_leakage_gate(X_train, y_train, X_val, y_val)

    results: dict = {"leakage_gate": leakage_result, "models": {}}

    # ── Model 1: Calibrated Logistic Regression ────────────────────────────────
    print("[Train] Training LogisticRegression (Platt calibrated)...")
    base_lr = LogisticRegression(max_iter=1000, C=1.0, random_state=42, class_weight="balanced")
    lr_cal = CalibratedClassifierCV(base_lr, method="sigmoid", cv=5)
    lr_cal.fit(X_train, y_train)
    lr_proba = lr_cal.predict_proba(X_val)[:, 1]
    lr_auc = roc_auc_score(y_val, lr_proba)
    print(f"  LR AUC (actual_recovered) = {lr_auc:.4f}")

    # Also evaluate on optimal action label
    lr_action_proba = lr_cal.predict_proba(X_val)[:, 1]
    lr_action_auc = roc_auc_score(y_val_action, lr_action_proba)

    import pickle

    with open(output_dir / "model_lr.pkl", "wb") as f:
        pickle.dump(lr_cal, f)

    results["models"]["logistic_regression"] = {
        "auc_actual_recovered": round(lr_auc, 4),
        "auc_optimal_action": round(lr_action_auc, 4),
    }

    # ── Model 2: LightGBM (primary) ────────────────────────────────────────────
    print("[Train] Training LightGBM...")
    # For LGBM: use raw features (string categoricals) for native handling
    cat_cols_present = [c for c in CATEGORICAL_COLS if c in feat_train.columns]

    # LightGBM requires pandas 'category' dtype for native categorical handling
    lgb_feat_train = feat_train.drop(columns=["episode_id"]).copy()
    lgb_feat_val = feat_val.drop(columns=["episode_id"]).copy()
    for col in cat_cols_present:
        lgb_feat_train[col] = lgb_feat_train[col].astype("category")
        lgb_feat_val[col] = lgb_feat_val[col].astype("category")

    lgb_train = lgb.Dataset(
        lgb_feat_train,
        label=y_train,
        categorical_feature=cat_cols_present,
        free_raw_data=False,
    )
    lgb_val_ds = lgb.Dataset(
        lgb_feat_val,
        label=y_val,
        categorical_feature=cat_cols_present,
        reference=lgb_train,
    )

    lgb_params = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_child_samples": 20,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "verbose": -1,
        "random_state": 42,
        "class_weight": "balanced",
    }

    callbacks = [
        lgb.early_stopping(stopping_rounds=50, verbose=False),
        lgb.log_evaluation(period=100),
    ]
    lgb_model = lgb.train(
        lgb_params,
        lgb_train,
        num_boost_round=500,
        valid_sets=[lgb_val_ds],
        callbacks=callbacks,
    )

    lgb_proba = lgb_model.predict(lgb_feat_val)
    lgb_auc = roc_auc_score(y_val, lgb_proba)
    lgb_action_auc = roc_auc_score(y_val_action, lgb_proba)
    print(f"  LightGBM AUC (actual_recovered) = {lgb_auc:.4f}")

    lgb_model.save_model(str(output_dir / "model_lightgbm.txt"))

    results["models"]["lightgbm"] = {
        "auc_actual_recovered": round(lgb_auc, 4),
        "auc_optimal_action": round(lgb_action_auc, 4),
        "best_iteration": lgb_model.best_iteration,
        "feature_importances": {
            name: int(imp)
            for name, imp in zip(
                lgb_model.feature_name(),
                lgb_model.feature_importance(importance_type="gain"),
            )
        },
    }

    # ── Model 3: MLPClassifier ─────────────────────────────────────────────────
    print("[Train] Training MLP [64, 32]...")
    mlp = MLPClassifier(
        hidden_layer_sizes=(64, 32),
        activation="relu",
        solver="adam",
        max_iter=200,
        early_stopping=True,
        validation_fraction=0.1,
        random_state=42,
        verbose=False,
    )
    mlp.fit(X_train, y_train)
    mlp_proba = mlp.predict_proba(X_val)[:, 1]
    mlp_auc = roc_auc_score(y_val, mlp_proba)
    mlp_action_auc = roc_auc_score(y_val_action, mlp_proba)
    print(f"  MLP AUC (actual_recovered) = {mlp_auc:.4f}")

    with open(output_dir / "model_mlp.pkl", "wb") as f:
        pickle.dump(mlp, f)

    results["models"]["mlp"] = {
        "auc_actual_recovered": round(mlp_auc, 4),
        "auc_optimal_action": round(mlp_action_auc, 4),
        "n_iter": mlp.n_iter_,
    }

    # Suspicion flag (Gate 3 — not a hard gate)
    max_auc = max(lgb_auc, mlp_auc, lr_auc)
    if max_auc > 0.88:
        print(
            f"[Gate3] WARNING: Max AUC = {max_auc:.4f} > 0.88. "
            f"Investigate for latent column leakage in features.parquet."
        )
        results["gate3_suspicion"] = True
    else:
        results["gate3_suspicion"] = False

    results["duration_sec"] = round(time.time() - t0, 2)
    results["best_model"] = max(
        results["models"].items(), key=lambda x: x[1]["auc_actual_recovered"]
    )[0]

    out_path = output_dir / "train_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    print(
        f"\n[Train] ✓ Complete in {results['duration_sec']}s | Best model: {results['best_model']}"
    )
    print(f"[Train] Results → {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Train recovery propensity models")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--output-dir", type=Path, default=ARTIFACTS_DIR)
    args = parser.parse_args()
    train(args.data_dir, args.output_dir)


if __name__ == "__main__":
    main()
