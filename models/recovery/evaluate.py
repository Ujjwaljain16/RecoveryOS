"""
Recovery model evaluation harness.

Three evaluation regimes:
    1. val_random   — generalization across synthetic population
    2. val_temporal — generalization to future conditions (later clock timestamps)
    3. test_temporal — final holdout (run once, results locked)

Metrics per regime:
    Propensity:  AUC-ROC, AUC-PR, Brier score, ECE, Precision@20%, Recall@20%
    Decision:    optimal-action accuracy, RETRY_NOW precision/recall, false-positive retry rate
    Economic:    gross_recovered_value, total_retry_cost, net_recovery_value, recovery_ROI

Scenario-level breakdown:
    Per failure class: AUC, Precision@20%, revenue captured, false-positive rate

Usage:
    python -m models.recovery.evaluate --data-dir=data --split=val_random
    python -m models.recovery.evaluate --data-dir=data --split=test_temporal  # run once only
"""

from __future__ import annotations

import argparse
import json
import time
import warnings
from pathlib import Path
from typing import Any

import numpy as np

try:
    import pandas as pd
    from sklearn.metrics import (
        average_precision_score,
        brier_score_loss,
        precision_score,
        recall_score,
        roc_auc_score,
    )
    import lightgbm as lgb
    HAS_DEPS = True
except ImportError as e:
    HAS_DEPS = False
    _IMPORT_ERROR = str(e)

from models.recovery.features import FeatureTransformer, CATEGORICAL_COLS

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
RECOVERY_MARGIN = 0.15
FIXED_RETRY_COST_PAISE = 100
VARIABLE_RETRY_COST_RATE = 0.001
BOOTSTRAP_N = 1000
BOOTSTRAP_SEED = 42


def _retry_cost(amount_paise: int) -> int:
    return FIXED_RETRY_COST_PAISE + int(amount_paise * VARIABLE_RETRY_COST_RATE)


def _bootstrap_metric(y_true: np.ndarray, y_score: np.ndarray, metric_fn, n: int = BOOTSTRAP_N) -> tuple[float, float]:
    rng = np.random.RandomState(BOOTSTRAP_SEED)
    vals = []
    for _ in range(n):
        idx = rng.choice(len(y_true), len(y_true), replace=True)
        yt, ys = y_true[idx], y_score[idx]
        if len(np.unique(yt)) < 2:
            continue
        try:
            vals.append(metric_fn(yt, ys))
        except Exception:
            pass
    vals = np.array(vals)
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def _expected_calibration_error(y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 10) -> float:
    bin_edges = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    for i in range(n_bins):
        mask = (y_prob >= bin_edges[i]) & (y_prob < bin_edges[i + 1])
        if mask.sum() == 0:
            continue
        acc = y_true[mask].mean()
        conf = y_prob[mask].mean()
        ece += mask.sum() * abs(acc - conf)
    return ece / len(y_true)


def _precision_at_k(y_true: np.ndarray, y_score: np.ndarray, k_fraction: float = 0.20) -> float:
    k = max(1, int(len(y_true) * k_fraction))
    top_k = np.argsort(y_score)[-k:]
    return float(y_true[top_k].mean())


def _recall_at_k(y_true: np.ndarray, y_score: np.ndarray, k_fraction: float = 0.20) -> float:
    k = max(1, int(len(y_true) * k_fraction))
    top_k = np.argsort(y_score)[-k:]
    total_pos = y_true.sum()
    if total_pos == 0:
        return 0.0
    return float(y_true[top_k].sum() / total_pos)


def _compute_economic_metrics(
    y_pred_binary: np.ndarray,
    y_actual_recovered: np.ndarray,
    amounts_paise: np.ndarray,
) -> dict:
    """
    Correct revenue formula:
        gross_recovered = Σ(amount × MARGIN) for predicted=RETRY_NOW AND actual_recovered=True
        total_retry_cost = Σ(fixed + variable × amount) for ALL predicted=RETRY_NOW
        net_recovery_value = gross_recovered - total_retry_cost
    """
    predicted_retry = y_pred_binary == 1
    true_positives = predicted_retry & (y_actual_recovered == 1)
    false_positives = predicted_retry & (y_actual_recovered == 0)

    gross_recovered_paise = int(np.sum(amounts_paise[true_positives] * RECOVERY_MARGIN))
    total_retry_cost_paise = int(np.sum([
        _retry_cost(int(a)) for a in amounts_paise[predicted_retry]
    ]))
    net_recovery_value_paise = gross_recovered_paise - total_retry_cost_paise
    roi = (net_recovery_value_paise / total_retry_cost_paise) if total_retry_cost_paise > 0 else 0.0

    return {
        "gross_recovered_rupees": round(gross_recovered_paise / 100, 2),
        "total_retry_cost_rupees": round(total_retry_cost_paise / 100, 2),
        "net_recovery_value_rupees": round(net_recovery_value_paise / 100, 2),
        "recovery_roi": round(roi, 4),
        "n_retry_predicted": int(predicted_retry.sum()),
        "n_true_positives": int(true_positives.sum()),
        "n_false_positives": int(false_positives.sum()),
        "false_positive_retry_rate": round(false_positives.sum() / max(1, (~y_actual_recovered.astype(bool)).sum()), 4),
    }


def evaluate_split(
    features_df: "pd.DataFrame",
    labels_df: "pd.DataFrame",
    lgb_model,
    transformer: FeatureTransformer,
    split_name: str,
) -> dict:
    """Run full evaluation for one split."""
    y_actual = labels_df["actual_recovered"].values.astype(int)
    y_action = (labels_df["optimal_recovery_action"] == "RETRY_NOW").values.astype(int)
    amounts = features_df["amount_paise"].values.astype(float)
    failure_classes = features_df["initial_failure_class"].values

    # LightGBM predictions (native categoricals)
    lgb_feat = features_df.drop(columns=["episode_id"]).copy()
    cat_cols = [c for c in CATEGORICAL_COLS if c in lgb_feat.columns]
    for col in cat_cols:
        lgb_feat[col] = lgb_feat[col].astype("category")

    lgb_proba = lgb_model.predict(lgb_feat)
    
    # Economically optimal dynamic threshold: E[retry] > 0 => P > cost / (amount * margin)
    retry_costs = np.array([_retry_cost(int(a)) for a in amounts])
    gross_rewards = amounts * RECOVERY_MARGIN
    # Avoid division by zero
    optimal_thresholds = np.where(gross_rewards > 0, retry_costs / gross_rewards, 1.0)
    # Threshold must be within [0, 1]
    optimal_thresholds = np.clip(optimal_thresholds, 0.0, 1.0)
    
    lgb_binary = (lgb_proba > optimal_thresholds).astype(int)

    # Also load LR for comparison
    import pickle
    lr_path = ARTIFACTS_DIR / "model_lr.pkl"
    lr_proba = None
    if lr_path.exists():
        with open(lr_path, "rb") as f:
            lr_model = pickle.load(f)
        X_transformed = transformer.transform(features_df)
        lr_proba = lr_model.predict_proba(X_transformed)[:, 1]

    auc = roc_auc_score(y_actual, lgb_proba)
    auc_pr = average_precision_score(y_actual, lgb_proba)
    brier = brier_score_loss(y_actual, lgb_proba)
    ece = _expected_calibration_error(y_actual, lgb_proba)
    prec_at_20 = _precision_at_k(y_actual, lgb_proba, 0.20)
    rec_at_20 = _recall_at_k(y_actual, lgb_proba, 0.20)
    auc_ci = _bootstrap_metric(y_actual, lgb_proba, roc_auc_score)

    # Decision metrics
    action_acc = float((lgb_binary == y_action).mean())
    retry_prec = precision_score(y_action, lgb_binary, zero_division=0)
    retry_rec = recall_score(y_action, lgb_binary, zero_division=0)

    # Economic metrics
    econ = _compute_economic_metrics(lgb_binary, y_actual, amounts.astype(int))

    # Scenario-level breakdown by initial_failure_class
    scenario_metrics: dict[str, Any] = {}
    for cls in np.unique(failure_classes):
        mask = failure_classes == cls
        if mask.sum() < 10:
            continue
        sub_actual = y_actual[mask]
        sub_proba = lgb_proba[mask]
        sub_binary = lgb_binary[mask]
        sub_amounts = amounts[mask].astype(int)
        if len(np.unique(sub_actual)) < 2:
            continue
        sub_econ = _compute_economic_metrics(sub_binary, sub_actual, sub_amounts)
        scenario_metrics[str(cls)] = {
            "n": int(mask.sum()),
            "auc": round(roc_auc_score(sub_actual, sub_proba), 4),
            "precision_at_20pct": round(_precision_at_k(sub_actual, sub_proba, 0.20), 4),
            "revenue_net_rupees": sub_econ["net_recovery_value_rupees"],
            "false_positive_retry_rate": sub_econ["false_positive_retry_rate"],
        }

    return {
        "split": split_name,
        "n": len(y_actual),
        "propensity_metrics": {
            "lgbm_auc_roc": round(auc, 4),
            "lgbm_auc_roc_ci_95": [round(auc_ci[0], 4), round(auc_ci[1], 4)],
            "lgbm_auc_pr": round(auc_pr, 4),
            "lgbm_brier_score": round(brier, 4),
            "lgbm_ece": round(ece, 4),
            "lgbm_precision_at_20pct": round(prec_at_20, 4),
            "lgbm_recall_at_20pct": round(rec_at_20, 4),
            **({"lr_auc_roc": round(roc_auc_score(y_actual, lr_proba), 4)} if lr_proba is not None else {}),
        },
        "decision_metrics": {
            "optimal_action_accuracy": round(action_acc, 4),
            "retry_now_precision": round(float(retry_prec), 4),
            "retry_now_recall": round(float(retry_rec), 4),
        },
        "economic_metrics": econ,
        "scenario_breakdown": scenario_metrics,
    }


def main() -> None:
    if not HAS_DEPS:
        raise ImportError(f"Missing dependencies: {_IMPORT_ERROR}")

    parser = argparse.ArgumentParser(description="Evaluate recovery propensity model")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument(
        "--split",
        choices=["val_random", "val_temporal", "test_random", "test_temporal"],
        default="val_random",
    )
    parser.add_argument("--artifacts-dir", type=Path, default=ARTIFACTS_DIR)
    args = parser.parse_args()

    print(f"[Evaluate] Loading model and split: {args.split}")
    lgb_model = lgb.Booster(model_file=str(args.artifacts_dir / "model_lightgbm.txt"))
    transformer = FeatureTransformer.load(args.artifacts_dir / "feature_transformer_v1.pkl")

    features = pd.read_parquet(args.data_dir / args.split / "features.parquet")
    labels = pd.read_parquet(args.data_dir / args.split / "labels.parquet")

    t0 = time.time()
    results = evaluate_split(features, labels, lgb_model, transformer, args.split)
    results["evaluation_duration_sec"] = round(time.time() - t0, 2)

    out_path = args.artifacts_dir / f"eval_{args.split}.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\n[Evaluate] {args.split} Results:")
    pm = results["propensity_metrics"]
    em = results["economic_metrics"]
    print(f"  AUC-ROC: {pm['lgbm_auc_roc']} (95% CI: {pm['lgbm_auc_roc_ci_95']})")
    print(f"  AUC-PR:  {pm['lgbm_auc_pr']} | Brier: {pm['lgbm_brier_score']} | ECE: {pm['lgbm_ece']}")
    print(f"  Precision@20%: {pm['lgbm_precision_at_20pct']} | Recall@20%: {pm['lgbm_recall_at_20pct']}")
    print(f"  Economic: Gross=₹{em['gross_recovered_rupees']:,.0f} | Cost=₹{em['total_retry_cost_rupees']:,.0f} | Net=₹{em['net_recovery_value_rupees']:,.0f} | ROI={em['recovery_roi']:.2%}")
    print(f"\n[Evaluate] ✓ Results → {out_path}")


if __name__ == "__main__":
    main()
