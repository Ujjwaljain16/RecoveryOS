"""
EpisodeGenerator: extends PaymentGenerator to produce full recovery episodes.

Each failed payment becomes an episode: simulate up to max_retries additional
attempts through the latent world. Patience decays, bank health evolves per-attempt.
Both ground-truth labels are derived from latent state — never from visible features.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Sequence

from simulator.core.clock import SimClock
from simulator.core.ids import DeterministicIdGenerator
from simulator.core.rng import SimRng
from simulator.customers.generator import SimulatedCustomer
from simulator.episodes.models import (
    RECOVERY_MARGIN,
    EpisodeBatchResult,
    RecoveryEpisode,
    RetryAttempt,
    compute_retry_cost,
    derive_optimal_action,
)
from simulator.failures.codes import ObservedFailureClass, TrueFailureType
from simulator.failures.observation_noise import ObservationNoisePipeline
from simulator.failures.protocol import PaymentContext, ScenarioModifier
from simulator.merchants.models import SimulatedMerchant
from simulator.outcomes.ground_truth import LatentRecoverabilityFunction
from simulator.payments.distributions import PaymentDistributionSampler
from simulator.payments.generator import PaymentGenerator, SimulatedPaymentRecord


# ─── Configuration ──────────────────────────────────────────────────────────────
MAX_RETRIES: int = 3
MIN_RETRY_DELAY_SEC: int = 60
MAX_RETRY_DELAY_SEC: int = 300


def _compute_ltv_decile(lifetime_value_paise: int, decile_cuts: list[int]) -> int:
    """Map raw LTV paise to decile [1..10] using pre-computed train-only cuts."""
    for i, cut in enumerate(decile_cuts):
        if lifetime_value_paise <= cut:
            return i + 1
    return 10


class EpisodeGenerator:
    """
    Generates recovery episodes from simulated failed payments.

    For each failed payment:
    1. Compute latent state at decision time (attempt 1)
    2. Derive optimal_recovery_action and expected_value from latent state
    3. Simulate up to MAX_RETRIES additional attempts through the latent world
    4. Record actual_outcome from the simulated chain
    5. Derive actual_recovered = (actual_outcome == "RECOVERED")

    The model gets: visible_features at decision time (after attempt 1)
    The evaluator gets: actual_recovered + optimal_recovery_action + latent state
    """

    def __init__(
        self,
        payment_generator: PaymentGenerator,
        id_gen: DeterministicIdGenerator,
        rng: SimRng,
        clock: SimClock,
        merchants: Sequence[SimulatedMerchant],
        customers: Sequence[SimulatedCustomer],
        scenarios: Sequence[ScenarioModifier],
        noise_pipeline: ObservationNoisePipeline,
        latent_function: LatentRecoverabilityFunction,
        ltv_decile_cuts: list[int] | None = None,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self.payment_generator = payment_generator
        self.id_gen = id_gen
        self.rng = rng
        self.clock = clock
        self.merchants = list(merchants)
        self.customers = list(customers)
        self.scenarios = list(scenarios)
        self.noise_pipeline = noise_pipeline
        self.latent_function = latent_function
        self.dist_sampler = PaymentDistributionSampler(rng)
        self.max_retries = max_retries
        # LTV decile cuts (must be fit on train split only — injected externally)
        # Default: uniform deciles of a typical Pareto LTV distribution
        self.ltv_decile_cuts: list[int] = ltv_decile_cuts or [
            5_000, 15_000, 35_000, 70_000, 120_000,
            200_000, 350_000, 600_000, 1_200_000, 999_999_999,
        ]
        self._customer_map = {c.customer_id: c for c in customers}

    def generate_episodes(
        self,
        n_failed_payments: int,
        simulation_id: str,
        split_name: str = "train",
    ) -> EpisodeBatchResult:
        """
        Generate n_failed_payments episodes. Each episode starts with a failed payment.
        Successful payments are skipped — episodes only exist for failures.
        """
        episodes: list[RecoveryEpisode] = []
        payment_idx = 0
        episode_idx = 0

        while len(episodes) < n_failed_payments:
            # Generate one payment candidate
            p_id = self.id_gen.payment_id(payment_idx)
            l_id = self.id_gen.latent_id(payment_idx)
            payment_idx += 1

            # Advance clock
            time_delta_sec = self.rng.uniform("payments", 5.0, 45.0)
            txn_time = self.clock.tick(time_delta_sec)

            # Select merchant + customer
            merchant = self.rng.choice("payments", self.merchants)
            merchant_customers = [c for c in self.customers if c.merchant_id == merchant.merchant_id]
            if not merchant_customers:
                merchant_customers = self.customers
            customer = self.rng.choice("payments", merchant_customers)

            # Sample payment attributes
            method = self.dist_sampler.sample_method()
            bank = self.dist_sampler.sample_bank()
            amount_paise = self.dist_sampler.sample_amount_paise(merchant, method)

            context = PaymentContext(
                payment_id=p_id,
                merchant=merchant,
                customer=customer,
                amount_paise=amount_paise,
                method=method,
                bank=bank,
                attempt_number=1,
                timestamp=txn_time,
            )

            # Evaluate composable scenarios
            failure_prob = 0.03
            latent_bank_health = 1.0
            active_scenarios: list[ScenarioModifier] = []
            scenario_rates: list[float] = []
            for scenario in self.scenarios:
                if scenario.applies_to(context):
                    active_scenarios.append(scenario)
                    rate = scenario.get_failure_rate(context)
                    scenario_rates.append(rate)
                    failure_prob = scenario.modify_failure_probability(failure_prob, context)
                    latent_bank_health = scenario.modify_latent_health(latent_bank_health, context)

            is_failed = self.rng.uniform("scenarios") < failure_prob
            if not is_failed:
                continue  # Only process failures as episodes

            # Determine true failure type at attempt 1
            if active_scenarios and sum(scenario_rates) > 0:
                chosen = self.rng.choices("scenarios", active_scenarios, weights=scenario_rates, k=1)[0]
                true_failure_type = chosen.determine_true_failure_type(context)
            else:
                true_failure_type = TrueFailureType.TRANSIENT_NETWORK_DROP

            # Observe with noise (telemetry for attempt 1)
            observed_1 = self.noise_pipeline.observe(true_failure_type)

            # ── Latent state at DECISION TIME (attempt 1) ─────────────────────
            _, latent_record_1 = self.latent_function.compute_latent_recovery(
                simulation_id=simulation_id,
                latent_id=l_id,
                payment_id=p_id,
                customer=customer,
                true_failure_type=true_failure_type,
                latent_bank_health=latent_bank_health,
                attempt_number=1,
                timestamp=txn_time,
            )

            true_recovery_prob_at_1 = latent_record_1.true_recovery_prob_bps / 10_000.0

            # ── Derive optimal action from attempt-1 latent state ─────────────
            optimal_action = derive_optimal_action(true_recovery_prob_at_1, amount_paise)
            expected_value = int(
                true_recovery_prob_at_1 * amount_paise * RECOVERY_MARGIN
                - compute_retry_cost(amount_paise)
            )

            # ── Simulate actual retry chain ────────────────────────────────────
            retries: list[RetryAttempt] = []
            actual_outcome: str = "ABANDONED"
            current_time = txn_time
            current_bank_health = latent_bank_health

            for attempt_num in range(2, self.max_retries + 2):
                # Delay before this retry
                delay_sec = self.rng.randint("payments", MIN_RETRY_DELAY_SEC, MAX_RETRY_DELAY_SEC)
                current_time = current_time + timedelta(seconds=delay_sec)

                # Re-evaluate scenarios at this point in time
                retry_context = PaymentContext(
                    payment_id=p_id,
                    merchant=merchant,
                    customer=customer,
                    amount_paise=amount_paise,
                    method=method,
                    bank=bank,
                    attempt_number=attempt_num,
                    timestamp=current_time,
                )
                retry_bank_health = 1.0
                retry_fail_prob = 0.03
                retry_scenarios: list[ScenarioModifier] = []
                retry_rates: list[float] = []
                for scenario in self.scenarios:
                    if scenario.applies_to(retry_context):
                        retry_scenarios.append(scenario)
                        retry_rates.append(scenario.get_failure_rate(retry_context))
                        retry_fail_prob = scenario.modify_failure_probability(retry_fail_prob, retry_context)
                        retry_bank_health = scenario.modify_latent_health(retry_bank_health, retry_context)

                # Compute latent state at this attempt (patience has decayed)
                retry_latent_id = f"{l_id}_r{attempt_num}"
                retry_true_failure = true_failure_type  # root cause persists
                _, latent_at_attempt = self.latent_function.compute_latent_recovery(
                    simulation_id=simulation_id,
                    latent_id=retry_latent_id,
                    payment_id=p_id,
                    customer=customer,
                    true_failure_type=retry_true_failure,
                    latent_bank_health=retry_bank_health,
                    attempt_number=attempt_num,
                    timestamp=current_time,
                )

                # Sample actual outcome at this attempt from latent probability
                retry_prob = latent_at_attempt.true_recovery_prob_bps / 10_000.0
                attempt_succeeds = self.rng.uniform("latent") < retry_prob

                if attempt_succeeds:
                    retries.append(RetryAttempt(
                        attempt_number=attempt_num,
                        delay_seconds=delay_sec,
                        observed_failure_code=None,
                        observed_failure_class=None,
                        outcome="SUCCESS",
                        occurred_at=current_time,
                        latent_patience_at_attempt=latent_at_attempt.customer_patience_score,
                        latent_bank_health_at_attempt=latent_at_attempt.bank_latent_health,
                        true_failure_type_at_attempt="SUCCESS",
                    ))
                    actual_outcome = "RECOVERED"
                    break
                else:
                    # Retry failed — observe with noise
                    observed_retry = self.noise_pipeline.observe(retry_true_failure)
                    retries.append(RetryAttempt(
                        attempt_number=attempt_num,
                        delay_seconds=delay_sec,
                        observed_failure_code=observed_retry.failure_code,
                        observed_failure_class=observed_retry.failure_class.value,
                        outcome="FAILED",
                        occurred_at=current_time,
                        latent_patience_at_attempt=latent_at_attempt.customer_patience_score,
                        latent_bank_health_at_attempt=latent_at_attempt.bank_latent_health,
                        true_failure_type_at_attempt=retry_true_failure.value,
                    ))
                    if attempt_num == self.max_retries + 1:
                        actual_outcome = "MAX_RETRIES_REACHED"

            total_duration = int((current_time - txn_time).total_seconds())
            ltv_decile = _compute_ltv_decile(customer.lifetime_value_paise, self.ltv_decile_cuts)

            episode_id = self.id_gen.event_id(episode_idx)
            episode_idx += 1

            episode = RecoveryEpisode(
                episode_id=episode_id,
                simulation_id=simulation_id,
                payment_id=p_id,
                amount_paise=amount_paise,
                method=method,
                bank=bank,
                merchant_id=merchant.merchant_id,
                customer_id=customer.customer_id,
                is_returning_customer=customer.is_returning,
                customer_ltv_decile=ltv_decile,
                initial_failure_code=observed_1.failure_code,
                initial_failure_class=observed_1.failure_class.value,
                hour_of_day=txn_time.hour,
                day_of_week=txn_time.weekday(),
                created_at=txn_time,
                clock_timestamp=txn_time,
                retries=tuple(retries),
                retry_count=len(retries),
                total_episode_duration_sec=total_duration,
                actual_outcome=actual_outcome,
                actual_recovered=(actual_outcome == "RECOVERED"),
                optimal_recovery_action=optimal_action,
                expected_value_of_retry_paise=expected_value,
                latent_patience_at_decision=latent_record_1.customer_patience_score,
                latent_bank_health_at_decision=latent_record_1.bank_latent_health,
                true_recovery_prob_bps_at_decision=latent_record_1.true_recovery_prob_bps,
                split_name=split_name,
            )
            episodes.append(episode)

        # Compute summary stats
        result = EpisodeBatchResult(simulation_id=simulation_id, episodes=episodes)
        result.total_failed_payments = len(episodes)
        result.actual_recovered_count = sum(1 for e in episodes if e.actual_recovered)
        result.retry_now_optimal_count = sum(1 for e in episodes if e.optimal_recovery_action == "RETRY_NOW")
        result.max_retries_reached_count = sum(1 for e in episodes if e.actual_outcome == "MAX_RETRIES_REACHED")
        result.abandoned_count = sum(1 for e in episodes if e.actual_outcome == "ABANDONED")
        return result
