"""
Generates the final Phase 2 Certificate ensuring all gates are passed.
Reads from models/recovery/artifacts/ and data/manifest.json.

Task MD2 (pre-Phase-8 audit): every field in the emitted certificate is
DERIVED from the values loaded here, never asserted as a literal. The
previous version hardcoded status="PASS", reproducibility=True,
test_set_frozen=True, latent_state_isolation=True regardless of what the
loaded gate results actually said — a future rerun where the leakage gate
genuinely failed would still have printed PASS. That's the exact
model-certification-side version of the rigged-eval failure mode TRD §7
was written to prevent on the revenue side.

Task MD3: the certificate also records which model production actually
uses (model_selected_for_production) and why -- see gaps.md §C.2:
train.py's own "best_model" pick (LightGBM) was selected on a
since-discovered-contaminated split; services/recovery_engine/propensity.py
loads the logistic regression instead. Anyone reading this file directly
should see that divergence without cross-referencing a second document.
"""

import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from models.recovery.evaluate import _compute_economic_metrics

# Mirrors models/recovery/train.py's own LEAKAGE_AUC_THRESHOLD -- restated
# here (not imported) so this module can derive a verdict from any
# train_results.json without requiring the full train.py import chain
# (sklearn/lightgbm) just to compute a threshold comparison.
LEAKAGE_AUC_THRESHOLD = 0.85

# gaps.md §C.2 -- ground-truth/latent columns the model must never see.
FORBIDDEN_FEATURE_COLUMNS = frozenset(
    {
        "actual_recovered",
        "optimal_recovery_action",
        "expected_value_of_retry_paise",
        "latent_patience_at_decision",
        "latent_bank_health_at_decision",
        "true_recovery_prob_bps_at_decision",
        "true_recovery_prob_bps",
        "ground_truth_recoverable",
        "customer_patience_score",
        "bank_latent_health",
        "latent_network_noise",
        "latent_customer_propensity",
    }
)

# gaps.md §C.2's fix: train.py's own "best_model" selection ran on
# val_random, later proven 58.8% duplicate-of-train rows. On the one split
# verified to have zero overlap with train (test_temporal), LightGBM does
# not clear TRD §3.3's >0.03-AUC-lift gate over LR, so LR is the correct
# certified production default. No retraining -- both artifacts already
# existed from Phase 2.
MODEL_SELECTED_FOR_PRODUCTION = "lr"
MODEL_SELECTION_REASON = (
    "train.py's best_model ('lightgbm') was picked on val_random, later found "
    "58.8% duplicate-of-train rows (gaps.md §C.2). On test_temporal -- the only "
    "split verified to have zero overlap with train -- LightGBM does not clear "
    "TRD §3.3's >0.03 AUC lift gate over LR (0.8374 vs 0.8378), so "
    "services/recovery_engine/propensity.py loads model_lr.pkl in production, "
    "not the model this certificate's 'best_model' field names."
)


def derive_leakage_gate_status(train_results: dict) -> str:
    """
    PASS/FAIL derived from the actual leakage-gate CI upper bound train.py
    computed, never a bare literal. Kept as a standalone pure function
    (train_results in, str out) so it's testable against a deliberately
    failing fixture without needing any real files on disk.
    """
    ci_hi = train_results["leakage_gate"]["ci_hi"]
    return "PASS" if ci_hi < LEAKAGE_AUC_THRESHOLD else "FAIL"


def _check_test_set_frozen(data_dir: Path) -> bool:
    """
    Verify test_temporal -- the split this certificate actually reports on
    -- has zero row overlap with train, by episode_id. gaps.md §C.2 found
    val_random/test_scenario contaminated this exact way (independently
    re-seeded RNG replaying the same episodes); this is the real check that
    would have caught it had the contamination ever reached the reporting
    split instead.
    """
    train_ids = set(pd.read_parquet(data_dir / "train" / "features.parquet")["episode_id"])
    test_ids = set(pd.read_parquet(data_dir / "test_temporal" / "features.parquet")["episode_id"])
    return train_ids.isdisjoint(test_ids)


def _check_latent_state_isolation(data_dir: Path) -> bool:
    """Verify none of the ground-truth/latent columns the model must never
    see actually appear in the visible feature set it was certified on."""
    test_features = pd.read_parquet(data_dir / "test_temporal" / "features.parquet")
    return not (set(test_features.columns) & FORBIDDEN_FEATURE_COLUMNS)


def _check_reproducibility(artifacts_dir: Path) -> bool:
    """
    Verify the production artifact actually loads and is a genuine fitted
    estimator, not just that a file with the right name exists on disk.
    Task MD1: a fresh clone was silently missing model_lr.pkl and
    feature_transformer_v1.pkl entirely (blanket-gitignored) while this
    field kept claiming reproducibility=True.
    """
    lr_path = artifacts_dir / "model_lr.pkl"
    transformer_path = artifacts_dir / "feature_transformer_v1.pkl"
    if not (lr_path.exists() and transformer_path.exists()):
        return False
    with open(lr_path, "rb") as f:
        model = pickle.load(f)
    return hasattr(model, "predict_proba") and hasattr(model, "classes_")


def generate_certificate():
    data_dir = Path("data")
    artifacts_dir = Path("models/recovery/artifacts")

    # Load dataset manifest
    with open(data_dir / "manifest.json") as f:
        manifest = json.load(f)

    dataset_counts = {s["split_name"]: s["n_episodes"] for s in manifest["splits"]}

    # Load training results (for leakage gate)
    with open(artifacts_dir / "train_results.json") as f:
        train_results = json.load(f)

    # Load evaluations
    with open(artifacts_dir / "eval_val_random.json") as f:
        val_random = json.load(f)

    with open(artifacts_dir / "eval_test_temporal.json") as f:
        test_temporal = json.load(f)

    with open(artifacts_dir / "eval_test_scenario.json") as f:
        test_scenario = json.load(f)

    # Calculate a naive baseline for Economics: "Retry Every Failed Payment" on test_temporal
    test_features = pd.read_parquet(data_dir / "test_temporal" / "features.parquet")
    test_labels = pd.read_parquet(data_dir / "test_temporal" / "labels.parquet")

    y_actual = test_labels["actual_recovered"].values.astype(int)
    amounts = test_features["amount_paise"].values.astype(int)
    failure_classes = test_features["initial_failure_class"].values

    # Heuristic baseline: retry everything EXCEPT known hard failures
    baseline_pred = np.where(np.isin(failure_classes, ["PERMANENT", "INSUFFICIENT_FUNDS"]), 0, 1)
    baseline_econ = _compute_economic_metrics(baseline_pred, y_actual, amounts)

    leakage_status = derive_leakage_gate_status(train_results)
    test_set_frozen = _check_test_set_frozen(data_dir)
    latent_state_isolation = _check_latent_state_isolation(data_dir)
    reproducibility = _check_reproducibility(artifacts_dir)
    overall_status = (
        "PASS"
        if leakage_status == "PASS"
        and test_set_frozen
        and latent_state_isolation
        and reproducibility
        else "FAIL"
    )

    cert = {
        "phase": "phase_2",
        "status": overall_status,
        "dataset": {
            "train": dataset_counts.get("train", 0),
            "val_random": dataset_counts.get("val_random", 0),
            "val_temporal": dataset_counts.get("val_temporal", 0),
            "test_random": dataset_counts.get("test_random", 0),
            "test_temporal": dataset_counts.get("test_temporal", 0),
        },
        "leakage_gate": {
            "status": leakage_status,
            "auc": train_results["leakage_gate"]["lr_auc"],
            "ci_upper": train_results["leakage_gate"]["ci_hi"],
        },
        "model": {
            "primary": train_results["best_model"],
            "val_auc": val_random["propensity_metrics"]["lgbm_auc_roc"],
            "temporal_auc": test_temporal["propensity_metrics"]["lgbm_auc_roc"],
            "scenario_holdout_auc": test_scenario["propensity_metrics"]["lgbm_auc_roc"],
        },
        "model_selected_for_production": MODEL_SELECTED_FOR_PRODUCTION,
        "model_selection_reason": MODEL_SELECTION_REASON,
        "economics": {
            "baseline_net_value_rupees": baseline_econ["net_recovery_value_rupees"],
            "model_net_value_rupees": test_temporal["economic_metrics"][
                "net_recovery_value_rupees"
            ],
            "incremental_value_rupees": test_temporal["economic_metrics"][
                "net_recovery_value_rupees"
            ]
            - baseline_econ["net_recovery_value_rupees"],
        },
        "reproducibility": reproducibility,
        "test_set_frozen": test_set_frozen,
        "latent_state_isolation": latent_state_isolation,
    }

    with open("phase_2_certificate.json", "w") as f:
        json.dump(cert, f, indent=2)

    print(f"[Phase 2] Certificate generated: phase_2_certificate.json (status={overall_status})")
    print(f"  Model Net Value: ₹{cert['economics']['model_net_value_rupees']:,.2f}")
    print(f"  Baseline Net Value: ₹{cert['economics']['baseline_net_value_rupees']:,.2f}")
    print(f"  Incremental Value: ₹{cert['economics']['incremental_value_rupees']:,.2f}")
    print(
        f"  Production model: {cert['model_selected_for_production']} ({cert['model_selection_reason']})"
    )


if __name__ == "__main__":
    generate_certificate()
