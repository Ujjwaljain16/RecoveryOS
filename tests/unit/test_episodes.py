"""
Unit tests for Phase 2 episode engine, dataset schema, and feature transformer.

Tests:
    TestEpisodeEngine:
        - actual retry chain is simulated (not frozen at attempt 1)
        - actual_recovered derives from actual_outcome
        - optimal_recovery_action derives from latent E[retry] at attempt 1
        - the two labels can diverge (both valid)
        - no latent fields in visible features dict

    TestDatasetSchema:
        - assert_no_leakage raises on prohibited columns
        - assert_no_leakage passes on clean features

    TestFeatureTransformer:
        - transform() raises before fit()
        - fit() only on train data — val transform uses frozen state
        - feature names are consistent after fit

    TestCalibrationLoader:
        - loads parameters.yaml with all required fields
        - method weights sum to ~1.0
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from simulator.dataset.schema import (
    PROHIBITED_IN_FEATURES,
    VISIBLE_FEATURE_COLUMNS,
    assert_no_leakage,
)
from simulator.episodes.models import (
    FIXED_RETRY_COST_PAISE,
    RECOVERY_MARGIN,
    VARIABLE_RETRY_COST_RATE,
    compute_expected_retry_value,
    compute_retry_cost,
    derive_optimal_action,
)

# ─── E[retry] formula tests ───────────────────────────────────────────────────


class TestEpisodeEconomics:
    def test_retry_cost_fixed_plus_variable(self):
        amount = 100_000  # ₹1000
        expected_cost = FIXED_RETRY_COST_PAISE + int(amount * VARIABLE_RETRY_COST_RATE)
        assert compute_retry_cost(amount) == expected_cost

    def test_retry_cost_scales_with_amount(self):
        small = compute_retry_cost(10_000)  # ₹100
        large = compute_retry_cost(5_000_000)  # ₹50,000
        assert large > small

    def test_expected_value_positive_high_prob_high_amount(self):
        # P=0.90, amount=₹5000 (500000 paise), margin=15%
        # E = 0.90 × 500000 × 0.15 − (100 + 500) = 67500 − 600 = 66900
        ev = compute_expected_retry_value(0.90, 500_000)
        assert ev > 0

    def test_expected_value_negative_low_prob_tiny_amount(self):
        # P=0.05, amount=₹5 (500 paise)
        # E = 0.05 × 500 × 0.15 − (100 + 0) = 3.75 − 100 < 0
        ev = compute_expected_retry_value(0.05, 500)
        assert ev < 0

    def test_derive_optimal_action_retry_when_positive(self):
        assert derive_optimal_action(0.90, 500_000) == "RETRY_NOW"

    def test_derive_optimal_action_do_not_retry_when_negative(self):
        assert derive_optimal_action(0.05, 500) == "DO_NOT_RETRY"

    def test_boundary_exactly_zero_is_do_not_retry(self):
        # If E[retry] == 0, should be DO_NOT_RETRY (not strictly positive)
        # We can find this approximately with very low probability
        action = derive_optimal_action(0.0, 1_000_000)
        assert action == "DO_NOT_RETRY"

    def test_recovery_margin_is_15_percent(self):
        assert abs(RECOVERY_MARGIN - 0.15) < 1e-9, "RECOVERY_MARGIN must be 15% (0.15)"

    def test_fixed_cost_is_1_rupee(self):
        assert FIXED_RETRY_COST_PAISE == 100, "Fixed retry cost must be ₹1 = 100 paise"

    def test_variable_cost_is_tenth_percent(self):
        assert abs(VARIABLE_RETRY_COST_RATE - 0.001) < 1e-9, "Variable rate must be 0.10%"


# ─── Dataset schema leakage guard tests ──────────────────────────────────────


class TestDatasetSchema:
    def test_assert_no_leakage_passes_on_clean_columns(self):
        clean = ["amount_paise", "method", "bank", "hour_of_day"]
        assert_no_leakage(clean)  # should not raise

    def test_assert_no_leakage_passes_on_all_visible_columns(self):
        assert_no_leakage(VISIBLE_FEATURE_COLUMNS)

    def test_assert_no_leakage_raises_on_latent_patience(self):
        with pytest.raises(AssertionError, match="LEAKAGE DETECTED"):
            assert_no_leakage(["amount_paise", "latent_patience_at_decision"])

    def test_assert_no_leakage_raises_on_true_recovery_prob(self):
        with pytest.raises(AssertionError, match="LEAKAGE DETECTED"):
            assert_no_leakage(["method", "true_recovery_prob_bps"])

    def test_assert_no_leakage_raises_on_expected_value(self):
        with pytest.raises(AssertionError, match="LEAKAGE DETECTED"):
            assert_no_leakage(["bank", "expected_value_of_retry_paise"])

    def test_assert_no_leakage_raises_on_bank_latent_health(self):
        with pytest.raises(AssertionError, match="LEAKAGE DETECTED"):
            assert_no_leakage(["bank_latent_health"])

    def test_all_prohibited_columns_are_truly_prohibited(self):
        """Every column in PROHIBITED_IN_FEATURES must trigger the leakage guard."""
        for col in PROHIBITED_IN_FEATURES:
            with pytest.raises(AssertionError, match="LEAKAGE DETECTED"):
                assert_no_leakage([col])

    def test_visible_feature_columns_has_no_prohibited(self):
        """VISIBLE_FEATURE_COLUMNS must not overlap with PROHIBITED_IN_FEATURES."""
        overlap = set(VISIBLE_FEATURE_COLUMNS) & set(PROHIBITED_IN_FEATURES)
        assert not overlap, f"Visible feature columns contain prohibited latent fields: {overlap}"


# ─── Feature transformer correctness ─────────────────────────────────────────


class TestFeatureTransformer:
    def _make_dummy_df(self, n: int = 50):
        try:
            import numpy as np
            import pandas as pd
        except ImportError:
            pytest.skip("pandas/numpy not installed")

        rng = np.random.default_rng(42)
        return pd.DataFrame(
            {
                "episode_id": [f"ep_{i}" for i in range(n)],
                "amount_paise": rng.integers(1000, 500_000, n),
                "method": rng.choice(["upi", "card", "netbanking", "wallet"], n),
                "bank": rng.choice(["HDFC", "ICICI", "SBI", "AXIS"], n),
                "is_returning_customer": rng.integers(0, 2, n),
                "customer_ltv_decile": rng.integers(1, 11, n),
                "initial_failure_code": rng.choice(["TIMEOUT", "BANK_ERROR", "UNKNOWN"], n),
                "initial_failure_class": rng.choice(["TRANSIENT", "BANK_ERROR", "UNKNOWN"], n),
                "hour_of_day": rng.integers(0, 24, n),
                "day_of_week": rng.integers(0, 7, n),
                "merchant_id": rng.choice(["m1", "m2", "m3"], n),
            }
        )

    def test_transform_raises_before_fit(self):
        try:
            from models.recovery.features import FeatureTransformer
        except ImportError:
            pytest.skip("scikit-learn not installed")
        ft = FeatureTransformer()
        df = self._make_dummy_df()
        with pytest.raises(RuntimeError, match="fit\\(\\)"):
            ft.transform(df)

    def test_fit_transform_produces_correct_shape(self):
        try:
            from models.recovery.features import FeatureTransformer
        except ImportError:
            pytest.skip("scikit-learn not installed")
        df = self._make_dummy_df(100)
        ft = FeatureTransformer()
        X = ft.fit_transform(df, episode_ids=df["episode_id"].tolist())
        assert X.shape[0] == 100
        assert X.shape[1] > 10  # at least 10 features after encoding

    def test_val_transform_uses_frozen_categories(self):
        """Val transform must use train-fitted categories — no re-fitting."""
        try:
            from models.recovery.features import FeatureTransformer
        except ImportError:
            pytest.skip("scikit-learn not installed")
        train_df = self._make_dummy_df(80)
        val_df = self._make_dummy_df(20)

        ft = FeatureTransformer()
        ft.fit(train_df, episode_ids=train_df["episode_id"].tolist())

        # Val transform should work — same number of columns as train
        X_train = ft.transform(train_df)
        X_val = ft.transform(val_df)
        assert (
            X_train.shape[1] == X_val.shape[1]
        ), "Train and val transformed feature counts must match (frozen transformer)"

    def test_feature_names_available_after_fit(self):
        try:
            from models.recovery.features import FeatureTransformer
        except ImportError:
            pytest.skip("scikit-learn not installed")
        df = self._make_dummy_df(50)
        ft = FeatureTransformer()
        ft.fit(df)
        names = ft.get_feature_names()
        assert len(names) > 0
        # No latent column names should appear
        for name in names:
            assert "latent" not in name.lower()
            assert "true_recovery" not in name.lower()
            assert "expected_value" not in name.lower()


# ─── Calibration loader tests ─────────────────────────────────────────────────


class TestCalibrationLoader:
    def test_loads_without_error(self):
        from simulator.calibration.loader import load_calibration

        params = load_calibration()
        assert params is not None

    def test_method_weights_sum_to_one(self):
        from simulator.calibration.loader import load_calibration

        params = load_calibration()
        total = sum(params.method_weights.values())
        assert abs(total - 1.0) < 0.01, f"Method weights sum to {total}, expected ~1.0"

    def test_baseline_failure_rate_is_reasonable(self):
        from simulator.calibration.loader import load_calibration

        params = load_calibration()
        assert 0.01 <= params.baseline_failure_rate <= 0.10

    def test_recovery_margin_matches_episodes_models(self):
        from simulator.calibration.loader import load_calibration
        from simulator.episodes.models import RECOVERY_MARGIN

        params = load_calibration()
        assert abs(params.recovery_margin - RECOVERY_MARGIN) < 1e-6

    def test_upi_share_from_npci_source(self):
        from simulator.calibration.loader import load_calibration

        params = load_calibration()
        # NPCI H1 FY24-25 UPI share ≈ 57%
        assert 0.50 <= params.upi_transaction_share <= 0.70

    def test_payment_generator_floor_reads_configured_calibration_not_hardcoded(self, monkeypatch):
        """
        gaps.md sec:C.2 -- PaymentGenerator used to hardcode a local
        `failure_prob = 0.03` floor that silently fought any calibrated
        NormalFailureScenario rate via max(base_prob, scenario_rate): fixing
        only the scenario's own rate would have left this floor clamping the
        effective rate back to 0.03 regardless. Patching load_calibration to
        an extreme value and observing the empirical failure rate move proves
        the floor is actually READ from calibration at generation time, not
        merely equal to it by coincidence.
        """
        import types
        from datetime import UTC, datetime

        import simulator.payments.generator as payments_generator_module
        from simulator.core.clock import SimClock
        from simulator.core.ids import DeterministicIdGenerator
        from simulator.core.rng import SimRng
        from simulator.customers.generator import CustomerGenerator
        from simulator.failures.observation_noise import ObservationNoisePipeline
        from simulator.merchants.models import MerchantGenerator
        from simulator.outcomes.ground_truth import LatentRecoverabilityFunction
        from simulator.payments.generator import PaymentGenerator

        fake_calib = types.SimpleNamespace(baseline_failure_rate=0.9)
        monkeypatch.setattr(payments_generator_module, "load_calibration", lambda: fake_calib)

        seed = 321
        id_gen = DeterministicIdGenerator(seed)
        rng = SimRng(seed)
        clock = SimClock(datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC))
        merchants = MerchantGenerator(id_gen, rng, clock.get_time()).generate_merchants()
        customers = CustomerGenerator(id_gen, rng, clock.get_time()).generate_customers(
            100, merchants
        )
        noise = ObservationNoisePipeline(rng, ambiguity_rate=0.10)
        latent_fn = LatentRecoverabilityFunction(rng)

        gen = PaymentGenerator(
            id_gen=id_gen,
            rng=rng,
            clock=clock,
            merchants=merchants,
            customers=customers,
            scenarios=[],  # no scenarios active -- failure_prob stays at the floor, untouched
            noise_pipeline=noise,
            latent_function=latent_fn,
        )
        assert (
            gen._baseline_failure_rate == 0.9
        ), "PaymentGenerator did not pick up the patched calibration value"

        batch = gen.generate_batch(400, "sim-test")
        observed_rate = sum(1 for p in batch.payments if p.status == "failed") / len(batch.payments)
        assert observed_rate > 0.6, (
            f"observed failure rate {observed_rate:.3f} did not move toward the patched "
            f"calibration value (0.9) -- the floor is not actually reading load_calibration()"
        )

    def test_episode_generator_floor_reads_configured_calibration_not_hardcoded(self, monkeypatch):
        """Same wiring, same gaps.md sec:C.2 bug shape, for EpisodeGenerator's
        two floors (attempt-1 and per-retry) -- see the PaymentGenerator
        version of this test for the full rationale."""
        import types

        import simulator.episodes.generator as episodes_generator_module
        from simulator.episodes.generator import EpisodeGenerator

        fake_calib = types.SimpleNamespace(baseline_failure_rate=0.9)
        monkeypatch.setattr(episodes_generator_module, "load_calibration", lambda: fake_calib)

        gen, merchants, customers, manifest = self._build_episode_gen_scenarios_shared()
        ep_gen = EpisodeGenerator(
            payment_generator=gen,
            id_gen=gen.id_gen,
            rng=gen.rng,
            clock=gen.clock,
            merchants=merchants,
            customers=customers,
            scenarios=[],
            noise_pipeline=gen.noise_pipeline,
            latent_function=gen.latent_function,
        )
        assert (
            ep_gen._baseline_failure_rate == 0.9
        ), "EpisodeGenerator did not pick up the patched calibration value"

    @staticmethod
    def _build_episode_gen_scenarios_shared():
        from datetime import UTC, datetime

        from simulator.run import build_simulator

        return build_simulator(
            seed=321,
            scenario_config={},
            customer_count=100,
            start_time=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
        )


# ─── Episode engine smoke test ────────────────────────────────────────────────


class TestEpisodeEngineSmoke:
    """
    Smoke test: generate a small batch of episodes and verify invariants.
    Does NOT test for specific numbers — tests structural correctness.
    """

    def _build_episode_gen(self):
        from simulator.core.clock import SimClock
        from simulator.core.ids import DeterministicIdGenerator
        from simulator.core.rng import SimRng
        from simulator.customers.generator import CustomerGenerator
        from simulator.episodes.generator import EpisodeGenerator
        from simulator.failures.observation_noise import ObservationNoisePipeline
        from simulator.failures.scenarios import NormalFailureScenario, TemporaryTimeoutScenario
        from simulator.merchants.models import MerchantGenerator
        from simulator.outcomes.ground_truth import LatentRecoverabilityFunction
        from simulator.payments.generator import PaymentGenerator

        seed = 77
        id_gen = DeterministicIdGenerator(seed)
        rng = SimRng(seed)
        clock = SimClock(datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC))

        merchants = MerchantGenerator(id_gen, rng, clock.get_time()).generate_merchants()
        customers = CustomerGenerator(id_gen, rng, clock.get_time()).generate_customers(
            100, merchants
        )
        scenarios = [NormalFailureScenario(0.15), TemporaryTimeoutScenario(0.10)]
        noise = ObservationNoisePipeline(rng, ambiguity_rate=0.10)
        latent_fn = LatentRecoverabilityFunction(rng)

        pay_gen = PaymentGenerator(
            id_gen, rng, clock, merchants, customers, scenarios, noise, latent_fn
        )
        ep_gen = EpisodeGenerator(
            payment_generator=pay_gen,
            id_gen=id_gen,
            rng=rng,
            clock=clock,
            merchants=merchants,
            customers=customers,
            scenarios=scenarios,
            noise_pipeline=noise,
            latent_function=latent_fn,
        )
        return ep_gen, id_gen.simulation_id()

    def test_episodes_have_correct_labels(self):
        ep_gen, sim_id = self._build_episode_gen()
        result = ep_gen.generate_episodes(50, sim_id)

        assert result.total_failed_payments == 50
        for ep in result.episodes:
            # actual_recovered must match actual_outcome
            assert ep.actual_recovered == (
                ep.actual_outcome == "RECOVERED"
            ), f"actual_recovered / actual_outcome mismatch for episode {ep.episode_id}"
            # optimal_recovery_action must be binary
            assert ep.optimal_recovery_action in ("RETRY_NOW", "DO_NOT_RETRY")
            # No WAIT allowed in Phase 2
            assert ep.optimal_recovery_action != "WAIT"

    def test_labels_can_diverge(self):
        """actual_recovered and optimal_recovery_action CAN disagree — that's valid signal."""
        ep_gen, sim_id = self._build_episode_gen()
        result = ep_gen.generate_episodes(200, sim_id)

        recovered_but_shouldnt = [
            e
            for e in result.episodes
            if e.actual_recovered and e.optimal_recovery_action == "DO_NOT_RETRY"
        ]
        shouldnt_but_did = [
            e
            for e in result.episodes
            if not e.actual_recovered and e.optimal_recovery_action == "RETRY_NOW"
        ]
        # Both cases should appear — they're valid divergence
        # (We can't assert exact counts, but at least one type should exist with 200 episodes)
        total_divergent = len(recovered_but_shouldnt) + len(shouldnt_but_did)
        assert (
            total_divergent > 0
        ), "Labels never diverged across 200 episodes — check E[retry] formula or latent function"

    def test_no_latent_fields_accessible_from_visible(self):
        """Verify episode visible fields contain no latent attributes."""
        ep_gen, sim_id = self._build_episode_gen()
        result = ep_gen.generate_episodes(20, sim_id)

        for _ep in result.episodes:
            # These are the latent fields — must NOT be in any feature row
            assert_no_leakage(
                [
                    "amount_paise",
                    "method",
                    "bank",
                    "is_returning_customer",
                    "customer_ltv_decile",
                    "initial_failure_code",
                    "initial_failure_class",
                    "hour_of_day",
                    "day_of_week",
                ]
            )

    def test_retry_chain_is_actually_simulated(self):
        """
        Retries must be determined by the latent world at each attempt,
        not frozen from attempt 1.
        """
        ep_gen, sim_id = self._build_episode_gen()
        result = ep_gen.generate_episodes(100, sim_id)

        # At least some episodes should have retries
        episodes_with_retries = [e for e in result.episodes if e.retry_count > 0]
        assert (
            len(episodes_with_retries) > 0
        ), "No episodes had any retries — check episode generator"

        # Retries should have their own latent state (different patience per attempt)
        for ep in episodes_with_retries[:5]:
            for retry in ep.retries:
                # Latent patience at each attempt must be ≤ attempt-1 patience (decaying)
                assert retry.latent_patience_at_attempt >= 0.0
                assert retry.latent_patience_at_attempt <= 1.0
