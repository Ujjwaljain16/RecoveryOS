"""
PaymentGenerator: orchestrates end-to-end payment generation in RecoveryOS Simulator.
Combines entity pools, distribution sampling, composable scenario modifiers,
observation noise injection, and latent ground truth computation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from simulator.calibration.loader import load_calibration
from simulator.core.clock import SimClock
from simulator.core.ids import DeterministicIdGenerator
from simulator.core.rng import SimRng
from simulator.customers.generator import SimulatedCustomer
from simulator.failures.codes import ObservedFailureClass, TrueFailureType
from simulator.failures.observation_noise import ObservationNoisePipeline
from simulator.failures.protocol import PaymentContext, ScenarioModifier
from simulator.merchants.models import SimulatedMerchant
from simulator.outcomes.ground_truth import LatentRecoverabilityFunction
from simulator.outcomes.latent_state import LatentStateRecord
from simulator.payments.distributions import PaymentDistributionSampler


@dataclass(frozen=True)
class SimulatedPaymentRecord:
    payment_id: str
    merchant_id: str
    customer_id: str
    amount_paise: int
    method: str
    bank: str
    status: str                         # 'failed' | 'success' | 'created'
    failure_code: str | None
    failure_class: str | None
    is_synthetic: bool
    ground_truth_recoverable: bool | None
    created_at: datetime
    failed_at: datetime | None


@dataclass(frozen=True)
class SimulatedEventRecord:
    event_id: str
    payment_id: str
    event_type: str                     # 'PAYMENT_CREATED' | 'PAYMENT_FAILED' | 'PAYMENT_SUCCESS'
    payload: dict
    occurred_at: datetime


@dataclass(frozen=True)
class GeneratedBatchResult:
    manifest_id: str
    payments: list[SimulatedPaymentRecord]
    events: list[SimulatedEventRecord]
    latent_records: list[LatentStateRecord]


class PaymentGenerator:
    """
    Generates a full batch of simulated payment transactions, events, and latent states.
    """

    def __init__(
        self,
        id_gen: DeterministicIdGenerator,
        rng: SimRng,
        clock: SimClock,
        merchants: Sequence[SimulatedMerchant],
        customers: Sequence[SimulatedCustomer],
        scenarios: Sequence[ScenarioModifier],
        noise_pipeline: ObservationNoisePipeline,
        latent_function: LatentRecoverabilityFunction,
    ):
        self.id_gen = id_gen
        self.rng = rng
        self.clock = clock
        self.merchants = list(merchants)
        self.customers = list(customers)
        self.scenarios = list(scenarios)
        self.noise_pipeline = noise_pipeline
        self.latent_function = latent_function
        self.dist_sampler = PaymentDistributionSampler(rng)
        # gaps.md sec:C.2 -- see EpisodeGenerator's identical comment: this
        # floor used to hardcode 0.03 and fight NormalFailureScenario's own
        # (calibrated) rate via max(base_prob, scenario_rate). Loaded once
        # here (lru_cache'd anyway), not per-payment.
        self._baseline_failure_rate = load_calibration().baseline_failure_rate

    def _sample_bank(self, timestamp: datetime) -> str:
        """
        Bank selection, consulting any active scenario's volume-concentration
        bias first (duck-typed `bank_concentration_bias` hook -- see
        BankDegradationScenario) before falling back to the calibrated
        overall market-share distribution. Checked BEFORE the normal sampler
        so a real degradation window's bank gets its realistic share of this
        run's traffic, not a diluted 1/N slice.
        """
        for scenario in self.scenarios:
            bias_fn = getattr(scenario, "bank_concentration_bias", None)
            if bias_fn is None:
                continue
            bias = bias_fn(timestamp)
            if bias is None:
                continue
            target_bank, concentration = bias
            if self.rng.uniform("payments") < concentration:
                return target_bank
        return self.dist_sampler.sample_bank()

    def generate_batch(self, count: int, simulation_id: str) -> GeneratedBatchResult:
        payments: list[SimulatedPaymentRecord] = []
        events: list[SimulatedEventRecord] = []
        latent_records: list[LatentStateRecord] = []

        # Customer lookup by ID
        customer_map = {c.customer_id: c for c in self.customers}
        # Merchant lookup by ID
        merchant_map = {m.merchant_id: m for m in self.merchants}

        event_counter = 0

        for idx in range(count):
            p_id = self.id_gen.payment_id(idx)
            l_id = self.id_gen.latent_id(idx)

            # Advance clock slightly for realistic temporal progression (5 to 45 seconds)
            time_delta_sec = self.rng.uniform("payments", 5.0, 45.0)
            txn_time = self.clock.tick(time_delta_sec)

            # 1. Select Merchant and Customer
            merchant = self.rng.choice("payments", self.merchants)
            # Pick customer from this merchant's customer cohort
            merchant_customers = [c for c in self.customers if c.merchant_id == merchant.merchant_id]
            if not merchant_customers:
                merchant_customers = self.customers
            customer = self.rng.choice("payments", merchant_customers)

            # 2. Sample method, bank, amount
            method = self.dist_sampler.sample_method()
            bank = self._sample_bank(txn_time)
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

            # 3. Evaluate active composable scenarios
            failure_prob = self._baseline_failure_rate
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

            # 4. Determine Failure outcome
            is_failed = self.rng.uniform("scenarios") < failure_prob

            if is_failed:
                # Sample root failure scenario proportionally from active scenarios
                if active_scenarios and sum(scenario_rates) > 0:
                    chosen_scenario = self.rng.choices(
                        "scenarios", active_scenarios, weights=scenario_rates, k=1
                    )[0]
                    true_failure_type = chosen_scenario.determine_true_failure_type(context)
                else:
                    true_failure_type = TrueFailureType.TRANSIENT_NETWORK_DROP

                # 5. Apply Observation Noise (Telemetry)
                observed = self.noise_pipeline.observe(true_failure_type)
                status = "failed"
                failure_code = observed.failure_code
                failure_class = observed.failure_class.value
                failed_at = txn_time
            else:
                true_failure_type = TrueFailureType.SUCCESS
                status = "success"
                failure_code = None
                failure_class = None
                failed_at = None

            # 6. Compute Latent Ground Truth Outcome
            is_recoverable, latent_record = self.latent_function.compute_latent_recovery(
                simulation_id=simulation_id,
                latent_id=l_id,
                payment_id=p_id,
                customer=customer,
                true_failure_type=true_failure_type,
                latent_bank_health=latent_bank_health,
                attempt_number=1,
                timestamp=txn_time,
            )

            latent_records.append(latent_record)

            payment_record = SimulatedPaymentRecord(
                payment_id=p_id,
                merchant_id=merchant.merchant_id,
                customer_id=customer.customer_id,
                amount_paise=amount_paise,
                method=method,
                bank=bank,
                status=status,
                failure_code=failure_code,
                failure_class=failure_class,
                is_synthetic=True,
                ground_truth_recoverable=is_recoverable if is_failed else None,
                created_at=txn_time,
                failed_at=failed_at,
            )
            payments.append(payment_record)

            # 7. Create Event Records
            e1_id = self.id_gen.event_id(event_counter)
            event_counter += 1
            events.append(
                SimulatedEventRecord(
                    event_id=e1_id,
                    payment_id=p_id,
                    event_type="PAYMENT_CREATED",
                    payload={
                        "merchant_id": merchant.merchant_id,
                        "customer_id": customer.customer_id,
                        "amount_paise": amount_paise,
                        "method": method,
                        "bank": bank,
                    },
                    occurred_at=txn_time,
                )
            )

            # Event: PAYMENT_FAILED or PAYMENT_SUCCESS
            e2_id = self.id_gen.event_id(event_counter)
            event_counter += 1
            if is_failed:
                events.append(
                    SimulatedEventRecord(
                        event_id=e2_id,
                        payment_id=p_id,
                        event_type="PAYMENT_FAILED",
                        payload={
                            "failure_code": failure_code,
                            "failure_class": failure_class,
                            "attempt_number": 1,
                        },
                        occurred_at=txn_time + timedelta(milliseconds=self.rng.randint("payments", 50, 800)),
                    )
                )
            else:
                events.append(
                    SimulatedEventRecord(
                        event_id=e2_id,
                        payment_id=p_id,
                        event_type="PAYMENT_SUCCESS",
                        payload={
                            "method": method,
                            "bank": bank,
                        },
                        occurred_at=txn_time + timedelta(milliseconds=self.rng.randint("payments", 100, 1200)),
                    )
                )

        return GeneratedBatchResult(
            manifest_id=simulation_id,
            payments=payments,
            events=events,
            latent_records=latent_records,
        )
