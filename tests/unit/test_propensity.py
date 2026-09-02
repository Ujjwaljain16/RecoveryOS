"""
Unit tests for services/recovery_engine/propensity.py — the production
adapter over Phase 2's certified logistic regression artifact (corrected
from LightGBM — see gaps.md §C.2 and propensity.py's module docstring for
why: LightGBM's reported AUC advantage was an artifact of a contaminated
validation split, and does not survive on the one genuinely clean holdout).
No DB needed: these test the pure inference path directly.
"""

from __future__ import annotations

import dataclasses
import pickle
from datetime import UTC, datetime

import pytest

from services.recovery_engine.propensity import (
    CATEGORICAL_FEATURES,
    FEATURE_ORDER,
    MODEL_ARTIFACT_PATH,
    TRANSFORMER_ARTIFACT_PATH,
    PropensityContext,
    _context_to_transformer_frame,
    build_propensity_context,
    predict_recovery_probability,
)


def _sample_context(**overrides) -> PropensityContext:
    defaults = {
        "amount_paise": 100_000,
        "method": "upi",
        "bank": "HDFC",
        "is_returning_customer": True,
        "customer_ltv_decile": 7,
        "initial_failure_code": "TIMEOUT",
        "initial_failure_class": "TEMPORARY",
        "hour_of_day": 9,
        "day_of_week": 3,
        "merchant_id": "not-a-training-merchant",
    }
    defaults.update(overrides)
    return PropensityContext(**defaults)


# ─── Mandatory test 1: production inference uses the certified artifact ───


def test_production_inference_uses_the_certified_artifact():
    assert MODEL_ARTIFACT_PATH.exists(), (
        f"Certified model artifact missing at {MODEL_ARTIFACT_PATH} — "
        f"predict_recovery_probability() has nothing real to load."
    )
    assert TRANSFORMER_ARTIFACT_PATH.exists()
    with open(MODEL_ARTIFACT_PATH, "rb") as f:
        model = pickle.load(f)
    # Genuine fitted sklearn estimator, not a placeholder — must expose
    # predict_proba and have been fit (classes_ only exists post-fit).
    assert hasattr(model, "predict_proba")
    assert hasattr(model, "classes_")


# ─── Mandatory test 2: production feature schema == Phase 2 training schema ─


def test_production_feature_schema_matches_transformer_schema():
    with open(TRANSFORMER_ARTIFACT_PATH, "rb") as f:
        transformer = pickle.load(f)
    expected = (
        set(transformer._present_cats)
        | set(transformer._present_cont)
        | {"hour_of_day", "day_of_week", "is_returning_customer", "customer_ltv_decile"}
    )
    assert (
        set(FEATURE_ORDER) == expected
    ), f"Schema drift: production sends {set(FEATURE_ORDER)}, transformer expects {expected}"


def test_categorical_features_are_a_subset_of_the_feature_order():
    assert set(CATEGORICAL_FEATURES).issubset(set(FEATURE_ORDER))


# ─── Mandatory test 3: frozen artifact is not refitted during inference ───


def test_predict_does_not_mutate_or_refit_the_model():
    from services.recovery_engine import propensity as propensity_module

    ctx = _sample_context()
    model1, transformer1 = propensity_module._load_artifacts()
    predict_recovery_probability(ctx)
    predict_recovery_probability(ctx)
    model2, transformer2 = propensity_module._load_artifacts()
    assert model1 is model2, "model must be a process-wide singleton, not reloaded per call"
    assert transformer1 is transformer2


# ─── Mandatory test 4: known examples produce stable predictions ─────────


def test_known_example_produces_stable_prediction():
    ctx = _sample_context()
    p1 = predict_recovery_probability(ctx)
    p2 = predict_recovery_probability(ctx)
    assert p1 == p2
    assert 0 <= p1.probability_bps <= 10_000


def test_permanent_failure_and_temporary_failure_get_different_probabilities():
    """Sanity: the model must actually be sensitive to failure_class, not
    return a constant regardless of input."""
    temporary = predict_recovery_probability(
        _sample_context(initial_failure_code="TIMEOUT", initial_failure_class="TEMPORARY")
    )
    permanent = predict_recovery_probability(
        _sample_context(initial_failure_code="TOKEN_REVOKED", initial_failure_class="PERMANENT")
    )
    assert temporary.probability_bps != permanent.probability_bps
    assert temporary.probability_bps > permanent.probability_bps


# ─── Mandatory test 5: no latent fields can enter inference ───────────────


def test_no_latent_fields_can_enter_inference():
    field_names = {f.name for f in dataclasses.fields(PropensityContext)}
    forbidden = {
        "actual_recovered",
        "true_recovery_prob",
        "true_recovery_prob_bps",
        "ground_truth_recoverable",
        "latent_patience_at_decision",
        "latent_bank_health_at_decision",
        "customer_patience_score",
        "bank_latent_health",
        "latent_network_noise",
        "latent_customer_propensity",
    }
    assert not (
        field_names & forbidden
    ), f"latent field(s) leaked into PropensityContext: {field_names & forbidden}"


def test_build_propensity_context_fails_loudly_on_missing_bank():
    with pytest.raises(ValueError):
        build_propensity_context(
            amount_paise=100_000,
            method="upi",
            bank=None,
            is_returning_customer=True,
            lifetime_value_paise=100_000,
            initial_failure_code="TIMEOUT",
            initial_failure_class="TEMPORARY",
            created_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
            merchant_id="m1",
        )


def test_build_propensity_context_fails_loudly_on_missing_failure_code():
    with pytest.raises(ValueError):
        build_propensity_context(
            amount_paise=100_000,
            method="upi",
            bank="HDFC",
            is_returning_customer=True,
            lifetime_value_paise=100_000,
            initial_failure_code=None,
            initial_failure_class="TEMPORARY",
            created_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
            merchant_id="m1",
        )


def test_ltv_decile_matches_phase2_fixed_cuts():
    from services.recovery_engine.propensity import compute_customer_ltv_decile

    # Phase 2's fixed cuts: [5000, 15000, 35000, 70000, 120000, 200000,
    # 350000, 600000, 1200000, 999999999] -> decile 1..10
    assert compute_customer_ltv_decile(1_000) == 1
    assert compute_customer_ltv_decile(6_000) == 2
    assert compute_customer_ltv_decile(2_000_000) == 10


# ─── Regression: the gating decision itself must be tested ────────────────


def test_lr_baseline_auc_reported():
    """Real number, from the actual frozen held-out split
    (models/recovery/artifacts/eval_test_temporal.json) — not fabricated."""
    import json
    from pathlib import Path

    eval_path = (
        Path(__file__).resolve().parent.parent.parent
        / "models"
        / "recovery"
        / "artifacts"
        / "eval_test_temporal.json"
    )
    with open(eval_path) as f:
        eval_data = json.load(f)
    lr_auc = eval_data["propensity_metrics"]["lr_auc_roc"]
    print(f"\n[LR baseline] test_temporal AUC = {lr_auc}")
    assert 0.5 < lr_auc < 1.0


def test_lgbm_does_not_beat_baseline_on_the_real_holdout_so_lr_stays_default():
    """
    The gating logic itself: TRD §3.3 requires LightGBM to beat LR by
    >0.03 AUC on a held-out fold before it replaces LR as the default.
    test_temporal is the only split verified to have zero row overlap with
    train (see gaps.md §C.2) — on THAT split, LightGBM does not clear the
    gate, so LR must remain the production default. This test fails loudly
    if the artifacts are ever regenerated in a way that reverses this.
    """
    import json
    from pathlib import Path

    from services.recovery_engine.propensity import MODEL_NAME

    eval_path = (
        Path(__file__).resolve().parent.parent.parent
        / "models"
        / "recovery"
        / "artifacts"
        / "eval_test_temporal.json"
    )
    with open(eval_path) as f:
        eval_data = json.load(f)
    lr_auc = eval_data["propensity_metrics"]["lr_auc_roc"]
    lgbm_auc = eval_data["propensity_metrics"]["lgbm_auc_roc"]
    lift = lgbm_auc - lr_auc

    print(f"\n[gate] lr_auc={lr_auc} lgbm_auc={lgbm_auc} lift={lift:.4f} (gate threshold=0.03)")

    gate_passes = lift > 0.03
    assert gate_passes is False, (
        "LightGBM unexpectedly cleared the >0.03 AUC gate on the clean holdout — "
        "if this is a real, re-verified improvement (not a re-introduced contaminated "
        "split), propensity.py should be switched back to the LightGBM path."
    )
    assert "logistic_regression" in MODEL_NAME, (
        "production model_name must reflect that LR (not LightGBM) is the certified default "
        "given the gate does not pass on clean data"
    )


# ─── gaps.md §B.1's remaining two named tests ───────────────────────────────


def test_feature_vector_only_contains_allowed_columns():
    """
    gaps.md §B.1: for a sweep of varied payments, the REAL feature vector
    built for the model (propensity.py::_context_to_transformer_frame --
    the actual DataFrame handed to the transformer, not just the
    PropensityContext dataclass one step removed from it) must contain
    ONLY FEATURE_ORDER's columns, nothing extra. test_no_latent_fields_can_
    enter_inference above checks a related but different claim (the
    dataclass's fields don't intersect a specific forbidden list) -- this
    is gaps.md's own positive allow-list version, against the actual
    model-facing artifact, swept across randomized inputs rather than one
    fixed sample.
    """
    import random

    rng = random.Random(20260902)
    banks = ("HDFC", "ICICI", "SBI", "AXIS", "KOTAK")
    methods = ("upi", "card", "netbanking", "wallet")
    failure_codes = ("TIMEOUT", "BANK_DOWN", "INVALID_CREDS", "INSUFFICIENT_FUNDS")
    failure_classes = ("TEMPORARY", "PERMANENT")

    for _ in range(100):
        context = build_propensity_context(
            amount_paise=rng.randint(100, 5_000_000),
            method=rng.choice(methods),
            bank=rng.choice(banks),
            is_returning_customer=rng.choice([True, False]),
            lifetime_value_paise=rng.randint(0, 2_000_000),
            initial_failure_code=rng.choice(failure_codes),
            initial_failure_class=rng.choice(failure_classes),
            created_at=datetime(2026, rng.randint(1, 12), rng.randint(1, 28), rng.randint(0, 23)),
            merchant_id=str(rng.randint(1, 3)),
        )

        # The dataclass itself, belt-and-suspenders (proven once per
        # iteration is enough -- it can't vary across instances of a frozen
        # dataclass with a fixed field set, but keeps this self-contained).
        assert {f.name for f in dataclasses.fields(context)} == set(FEATURE_ORDER)

        # The actual model-facing artifact.
        frame = _context_to_transformer_frame(context)
        assert set(frame.columns) == set(FEATURE_ORDER), (
            f"feature vector columns {sorted(frame.columns)} must exactly match "
            f"FEATURE_ORDER {sorted(FEATURE_ORDER)}, nothing extra, nothing missing"
        )


def test_model_auc_does_not_suspiciously_spike_after_feature_changes():
    """
    gaps.md §B.1's regression guard: if a future change to the feature
    pipeline pushes the certified model's reported AUC above a ceiling no
    honest feature set should cross, that's the signature of a leak
    (ground_truth_recoverable or an equivalent latent field sneaking back
    into inference), not a genuine improvement -- flag it for manual review
    rather than silently accepting a suspiciously good number.

    Recorded baseline (models/recovery/artifacts/eval_test_temporal.json,
    the one split verified to have zero overlap with train -- see gaps.md
    §C.2): LR AUC = 0.8378. The ceiling is deliberately generous (0.97,
    gaps.md's own suggested threshold) -- this is a tripwire for "something
    is badly wrong", not a tight tolerance band around the exact number.
    """
    import json

    eval_path = MODEL_ARTIFACT_PATH.parent / "eval_test_temporal.json"
    with open(eval_path) as f:
        eval_data = json.load(f)
    lr_auc = eval_data["propensity_metrics"]["lr_auc_roc"]

    print(f"\n[AUC regression guard] lr_auc={lr_auc} (suspicious-spike ceiling=0.97)")

    assert 0.5 < lr_auc <= 0.97, (
        f"LR AUC on the clean test_temporal holdout is {lr_auc}, outside the expected "
        f"[0.5, 0.97] range -- either below-chance (something is broken) or above the "
        f"suspicious-spike ceiling (a feature pipeline change likely reintroduced a "
        f"ground_truth_recoverable-style leak; this needs manual review before merge, "
        f"gaps.md §B.1)"
    )
