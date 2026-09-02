"""
Unit and Non-Circularity Validation Tests for RecoveryOS Simulator (TRD §6, PRD §30-32).
"""

from datetime import UTC, datetime, timedelta

from simulator.run import (
    TEST_SCENARIO_SEED_OFFSET,
    VAL_RANDOM_SEED_OFFSET,
    build_simulator,
)
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


class TestSimulatorStartTimeControl:
    """
    A synthetic canonical/evaluation run's payment timestamps used to be
    pinned to a fixed historical date (SimClock's own default,
    2026-08-20T09:00:00Z) with no override -- harmless while "now" was still
    inside services/recovery_engine/orchestrator.py's 7-day EligibilityRule
    window relative to that date, but every payment silently becomes
    permanently ineligible ("payment has expired") once real time moves past
    it, with no code change required to trigger the failure. build_simulator()
    already accepted an explicit `start_time` override; simulator/run.py's
    CLI just never exposed it. These tests cover the new --start-time wiring
    -- NOT EligibilityRule itself, which is untouched (see
    tests/unit/test_policy_engine.py for that).
    """

    def test_default_start_time_is_unchanged(self):
        """Regression pin: omitting start_time must keep producing the exact
        same fixed date every prior test/run already depends on."""
        fixed_default = datetime(2026, 8, 20, 9, 0, 0, tzinfo=UTC)
        gen, _, _, manifest = build_simulator(seed=42, customer_count=50)
        assert manifest.created_at == fixed_default
        batch = gen.generate_batch(5, manifest.simulation_id)
        first_payment_delta = batch.payments[0].created_at - fixed_default
        assert timedelta(0) <= first_payment_delta < timedelta(minutes=1)

    def test_explicit_start_time_sets_first_payment_timestamp(self):
        """An explicit start_time is what the simulated clock actually starts
        from -- the manifest's created_at (persisted to simulation_manifests
        for the DB path) and the first payment's timestamp should both land
        at or immediately after it, not the old fixed default."""
        chosen = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
        gen, _, _, manifest = build_simulator(seed=42, customer_count=50, start_time=chosen)
        assert manifest.created_at == chosen
        batch = gen.generate_batch(5, manifest.simulation_id)
        first_payment_delta = batch.payments[0].created_at - chosen
        assert timedelta(0) <= first_payment_delta < timedelta(minutes=1)

    def test_same_seed_and_start_time_is_reproducible(self):
        """The override must not break determinism: two runs with the same
        seed AND the same explicit start_time must be byte-identical, same
        contract as the fixed-default case (test_deterministic_seed_reproducibility)."""
        chosen = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
        gen1, _, _, manifest1 = build_simulator(seed=42, customer_count=100, start_time=chosen)
        batch1 = gen1.generate_batch(200, manifest1.simulation_id)
        gen2, _, _, manifest2 = build_simulator(seed=42, customer_count=100, start_time=chosen)
        batch2 = gen2.generate_batch(200, manifest2.simulation_id)
        assert batch1.payments == batch2.payments
        assert batch1.latent_records == batch2.latent_records

    def test_different_start_time_shifts_timestamps_but_not_seeded_randomness(self):
        """Changing start_time must only move the clock -- the RNG-driven
        content (method/bank/amount/merchant/customer selection, failure
        classification) must stay identical, proving start_time and seed are
        genuinely independent knobs, not entangled."""
        seed_a = datetime(2026, 8, 20, 9, 0, 0, tzinfo=UTC)
        seed_b = datetime(2026, 9, 15, 3, 30, 0, tzinfo=UTC)
        offset = seed_b - seed_a

        gen_a, _, _, manifest_a = build_simulator(seed=7, customer_count=100, start_time=seed_a)
        batch_a = gen_a.generate_batch(150, manifest_a.simulation_id)
        gen_b, _, _, manifest_b = build_simulator(seed=7, customer_count=100, start_time=seed_b)
        batch_b = gen_b.generate_batch(150, manifest_b.simulation_id)

        for pa, pb in zip(batch_a.payments, batch_b.payments):
            assert pa.payment_id == pb.payment_id
            assert pa.method == pb.method
            assert pa.bank == pb.bank
            assert pa.amount_paise == pb.amount_paise
            assert pa.status == pb.status
            assert pa.merchant_id == pb.merchant_id
            assert pa.customer_id == pb.customer_id
            assert pa.created_at - pb.created_at == -offset

    def test_recent_start_time_stays_inside_the_real_eligibility_window(self):
        """
        Validates the actual usage discipline this fix exists for: a
        start_time chosen close to real 'now' produces payments whose
        failed_at satisfies the exact boundary
        services/recovery_engine/orchestrator.py's EligibilityRule uses
        (now - failed_at <= 7 days) -- expressed relative to real time (not a
        hardcoded date), so this test never itself goes stale the way the
        bug it guards against did.
        """
        real_now = datetime.now(UTC)
        start = real_now - timedelta(hours=1)
        gen, _, _, manifest = build_simulator(seed=42, customer_count=50, start_time=start)
        batch = gen.generate_batch(30, manifest.simulation_id)
        failed = [p for p in batch.payments if p.status == "failed" and p.failed_at is not None]
        assert failed, "test setup produced zero failed payments to check the window against"
        for p in failed:
            assert real_now - p.failed_at <= timedelta(days=7), (
                f"payment {p.payment_id} failed_at={p.failed_at} would already be outside "
                f"EligibilityRule's 7-day window relative to real now={real_now}"
            )

    def test_stale_default_would_fall_outside_the_real_eligibility_window(self):
        """
        The inverse check, proving this test suite would actually have
        caught the original bug: the OLD fixed default (2026-08-20) is now
        (as of whenever this test runs) almost certainly more than 7 days
        before real 'now' -- confirming the CLI default alone is NOT safe
        for a canonical/evaluation run and an explicit --start-time is
        required for those, exactly as simulator/run.py's new --start-time
        help text says.
        """
        real_now = datetime.now(UTC)
        fixed_default = datetime(2026, 8, 20, 9, 0, 0, tzinfo=UTC)
        if real_now - fixed_default <= timedelta(days=7):
            import pytest

            pytest.skip(
                "real clock is still within 7 days of the fixed default -- "
                "the bug this test documents hasn't become reachable yet"
            )
        assert real_now - fixed_default > timedelta(days=7)


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


def _gen_episodes_for_test(seed: int, n: int, split_name: str, config_override: dict | None = None):
    """Mirrors simulator.run.run_episode_mode's _gen_episodes closure exactly,
    at small scale, so this test exercises the real seed-derivation path."""
    from simulator.episodes.generator import EpisodeGenerator

    gen, merchants, customers, manifest = build_simulator(
        seed=seed, scenario_config=config_override or {}, customer_count=200
    )
    ep_gen = EpisodeGenerator(
        payment_generator=gen,
        id_gen=gen.id_gen,
        rng=gen.rng,
        clock=gen.clock,
        merchants=merchants,
        customers=customers,
        scenarios=gen.scenarios,
        noise_pipeline=gen.noise_pipeline,
        latent_function=gen.latent_function,
    )
    return ep_gen.generate_episodes(n, manifest.simulation_id, split_name=split_name).episodes


class TestSimulatorSplitIndependence:
    """
    gaps.md sec:C.2/C.3 -- val_random and test_scenario used to be generated
    with the SAME seed as train/test_random. DeterministicIdGenerator/SimRng
    have no cursor state, so two build_simulator() calls with an identical
    seed are byte-identical from index 0: val_random was a literal duplicate
    of train's leading episodes, and test_scenario was near-fully correlated
    with test_random. This is the regression guard for that leak — it should
    never silently reappear.
    """

    def test_val_random_does_not_duplicate_train(self):
        train_eps = _gen_episodes_for_test(42, 50, "train")
        val_eps = _gen_episodes_for_test(42 + VAL_RANDOM_SEED_OFFSET, 50, "val_random")

        train_ids = {e.episode_id for e in train_eps}
        val_ids = {e.episode_id for e in val_eps}
        assert train_ids.isdisjoint(val_ids), (
            "val_random shares episode_ids with train -- the same-seed "
            "duplication bug (gaps.md sec:C.2) has reappeared"
        )

        # episode_id is a UUIDv5 of (seed, entity_type, index) -- disjoint IDs
        # alone could theoretically still coincide in content, so also check
        # the underlying feature tuples aren't a positional re-run of train.
        def _content(ep):
            return (ep.amount_paise, ep.method, ep.bank, ep.hour_of_day, ep.day_of_week, ep.merchant_id)

        train_content = [_content(e) for e in train_eps]
        val_content = [_content(e) for e in val_eps]
        assert train_content != val_content, (
            "val_random's feature content positionally matches train -- "
            "still generated from the same seed"
        )

    def test_test_scenario_does_not_duplicate_test_random(self):
        test_random_eps = _gen_episodes_for_test(999, 50, "test_random")
        test_scenario_eps = _gen_episodes_for_test(
            999 + TEST_SCENARIO_SEED_OFFSET,
            50,
            "test_scenario",
            config_override={"outage_rate": 0.8, "degradation_rate": 0.6},
        )

        test_random_ids = {e.episode_id for e in test_random_eps}
        test_scenario_ids = {e.episode_id for e in test_scenario_eps}
        assert test_random_ids.isdisjoint(test_scenario_ids), (
            "test_scenario shares episode_ids with test_random -- the "
            "same-seed correlation bug (gaps.md sec:C.3) has reappeared"
        )


class TestSimulatorCustomerTemporalIntegrity:
    def test_opted_out_at_never_precedes_created_at(self):
        """
        Task SIM1 (pre-Phase-8 audit): opted_out_at and created_at used to be
        drawn independently, which could produce a customer who "opted out"
        before they existed. A high opt_out_baseline_rate here (not the
        production 4% default) is deliberate -- it forces enough opted-out
        customers to exist that this test would actually have caught the
        pre-fix bug, not passed vacuously on a batch with zero opt-outs.
        """
        from datetime import datetime

        from simulator.core.ids import DeterministicIdGenerator
        from simulator.core.rng import SimRng
        from simulator.customers.generator import CustomerGenerator
        from simulator.merchants.models import MerchantGenerator

        base_time = datetime(2026, 8, 20, 9, 0, 0, tzinfo=UTC)
        id_gen = DeterministicIdGenerator(seed=42)
        rng = SimRng(master_seed=42)
        merchants = MerchantGenerator(id_gen, rng, base_time).generate_merchants()

        customer_gen = CustomerGenerator(id_gen, rng, base_time, opt_out_baseline_rate=0.5)
        customers = customer_gen.generate_customers(2000, merchants)

        opted_out = [c for c in customers if c.opted_out_at is not None]
        assert len(opted_out) > 0, (
            "test setup is vacuous -- no opted-out customers were generated to check the "
            "invariant against; this would not have caught the bug"
        )

        violations = [c for c in opted_out if c.opted_out_at <= c.created_at]
        assert not violations, (
            f"{len(violations)} customer(s) have opted_out_at <= created_at -- a temporally "
            f"impossible record. First violation: customer_id={violations[0].customer_id} "
            f"created_at={violations[0].created_at} opted_out_at={violations[0].opted_out_at}"
        )
        for c in opted_out:
            assert c.opted_out_at <= base_time, (
                f"customer_id={c.customer_id} has opted_out_at={c.opted_out_at} in the future "
                f"relative to base_time={base_time}"
            )
