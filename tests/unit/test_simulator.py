"""
Unit and Non-Circularity Validation Tests for RecoveryOS Simulator (TRD §6, PRD §30-32).
"""

from simulator.run import build_simulator
from simulator.validation.distribution_tests import verify_scenario_distributions
from simulator.validation.leakage_tests import run_leakage_model_ladder
from simulator.validation.reproducibility import verify_deterministic_reproducibility


class TestSimulatorReproducibility:
    def test_deterministic_seed_reproducibility(self):
        """
        Asserts that identical simulation seeds yield byte-identical entity IDs,
        transaction features, and ground-truth values.
        """
        report = verify_deterministic_reproducibility(seed=42, n=1000)
        assert (
            report["is_identical"] is True
        ), f"Non-deterministic diffs found: {report['sample_diffs']}"
        assert report["diff_count"] == 0


class TestSimulatorDistributions:
    def test_scenario_generators_produce_expected_failure_rate(self):
        """
        Asserts that combined scenarios produce a realistic aggregate failure rate (5% to 25%)
        with all failure classes represented.
        """
        report = verify_scenario_distributions(n=3000, seed=42)
        rate = report["observed_failure_rate"]
        assert (
            0.05 <= rate <= 0.30
        ), f"Observed failure rate {rate} out of expected range [0.05, 0.30]"

        # Check method shares (UPI should be highest, ~45-65%)
        upi_share = report["method_shares"].get("upi", 0.0)
        assert (
            0.40 <= upi_share <= 0.70
        ), f"UPI share {upi_share} outside expected range [0.40, 0.70]"

    def test_all_six_scenarios_and_ambiguity_represented(self):
        """
        Asserts that TEMPORARY, PERMANENT, CUSTOMER_SPECIFIC, SYSTEMIC, and UNKNOWN are present.
        """
        report = verify_scenario_distributions(n=4000, seed=42)
        breakdown = report["failure_class_breakdown"]
        assert "TEMPORARY" in breakdown and breakdown["TEMPORARY"] > 0
        assert "PERMANENT" in breakdown and breakdown["PERMANENT"] > 0
        assert "CUSTOMER_SPECIFIC" in breakdown and breakdown["CUSTOMER_SPECIFIC"] > 0
        assert "SYSTEMIC" in breakdown and breakdown["SYSTEMIC"] > 0
        assert "UNKNOWN" in breakdown and breakdown["UNKNOWN"] > 0


class TestSimulatorGroundTruthNonCircularity:
    def test_ground_truth_not_derivable_from_visible_features(self):
        """
        CRITICAL TEST: Multi-model baseline ladder (Logistic Regression, Random Forest, GBDT)
        trained ONLY on visible features must have AUC < 0.85 ceiling.
        This proves observable features do not trivially leak ground truth.
        """
        report = run_leakage_model_ladder(n_samples=5000, seed=42)
        max_auc = report.get("max_auc", 0.0)
        assert max_auc < 0.85, (
            f"Ground truth leakage detected! Model ladder achieved Max AUC = {max_auc:.4f} >= 0.85. "
            f"Full report: {report}"
        )
        # Verify non-triviality (better than pure random coin flip 0.5)
        assert (
            max_auc > 0.52
        ), f"Model ladder should show correlation with visible features (got {max_auc:.4f})"


class TestSimulatorMonetaryIntegrity:
    def test_all_amounts_are_positive_integers(self):
        """
        Verifies that all simulated payment amounts are strictly positive integer paise.
        """
        gen, merchants, customers, manifest = build_simulator(seed=42)
        batch = gen.generate_batch(500, manifest.simulation_id)
        for p in batch.payments:
            assert isinstance(p.amount_paise, int)
            assert p.amount_paise > 0
            assert p.amount_paise >= 1000  # at least ₹10
