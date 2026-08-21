"""
Dataset schema enforcement for RecoveryOS.

Defines the explicit allowlist of visible features and label columns.
DatasetBuilder uses this to enforce that features.parquet NEVER contains
latent or label columns. Tested explicitly in test_dataset_schema.py.
"""

from __future__ import annotations

# ─── Columns allowed in features.parquet ────────────────────────────────────────
# These are ALL the columns the model is permitted to see at inference time.
# Adding a column here requires explicit justification that it is observable
# at decision time (after attempt 1, before retry decision).
VISIBLE_FEATURE_COLUMNS: list[str] = [
    # Payment context
    "amount_paise",               # raw integer — transformed to log in FeatureTransformer
    "method",                     # categorical — OneHot in LR/MLP, native in LGBM
    "bank",                       # categorical — OneHot in LR/MLP, native in LGBM
    "is_returning_customer",      # binary int (0/1)
    "customer_ltv_decile",        # ordinal [1..10] — genuinely ordinal, binned on train only
    "initial_failure_code",       # categorical — observed telemetry
    "initial_failure_class",      # categorical — observed telemetry
    "hour_of_day",                # int [0..23] — cyclic-encoded in FeatureTransformer
    "day_of_week",                # int [0..6] — cyclic-encoded in FeatureTransformer
    "merchant_id",                # categorical (3 values) — OneHot in LR/MLP
]

# ─── Columns in labels.parquet ──────────────────────────────────────────────────
# These are the ground-truth labels. They are NEVER in features.parquet.
LABEL_COLUMNS: list[str] = [
    "episode_id",                 # join key — also in features
    "actual_recovered",           # bool — factual: did it recover within horizon?
    "optimal_recovery_action",    # str: RETRY_NOW | DO_NOT_RETRY
]

# ─── Columns that are latent — never in ANY model-accessible file ───────────────
# Listed here for auditability. These exist only in simulator_latent_state table
# and the RetryAttempt.latent_* fields (evaluator-only).
PROHIBITED_IN_FEATURES: list[str] = [
    "latent_patience_at_decision",
    "latent_bank_health_at_decision",
    "true_recovery_prob_bps_at_decision",
    "expected_value_of_retry_paise",
    "latent_patience_at_attempt",
    "latent_bank_health_at_attempt",
    "true_failure_type_at_attempt",
    "customer_patience_score",
    "bank_latent_health",
    "latent_network_noise",
    "latent_customer_propensity",
    "true_recovery_prob_bps",
    "true_failure_type",
]


def assert_no_leakage(df_columns: list[str], context: str = "") -> None:
    """
    Assert that a dataframe contains no prohibited latent or label columns.
    Called by DatasetBuilder before writing any features.parquet.
    Raises AssertionError with specific column names if violated.
    """
    prohibited = set(PROHIBITED_IN_FEATURES) | set(LABEL_COLUMNS) - {"episode_id"}
    leaked = set(df_columns) & prohibited
    if leaked:
        raise AssertionError(
            f"LEAKAGE DETECTED{' in ' + context if context else ''}: "
            f"prohibited columns found in features dataset: {sorted(leaked)}"
        )
