"""
CLI Entrypoint for RecoveryOS Simulator.
Usage:
    python -m simulator.run --n=10000 --seed=42
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from recoveryos.database import get_sync_engine
from recoveryos.models import (
    Customer,
    Event,
    Merchant,
    Payment,
    SimulationManifest,
    SimulatorLatentState,
)
from simulator.core.clock import SimClock
from simulator.core.ids import DeterministicIdGenerator
from simulator.core.manifest import SimulationManifestData
from simulator.core.rng import SimRng
from simulator.customers.generator import CustomerGenerator, SimulatedCustomer
from simulator.failures.observation_noise import ObservationNoisePipeline
from simulator.failures.scenarios import (
    BankDegradationScenario,
    CustomerRepeatFailureScenario,
    MultiRailOutageScenario,
    NormalFailureScenario,
    PermanentFailureScenario,
    TemporaryTimeoutScenario,
)
from simulator.merchants.models import MerchantGenerator, SimulatedMerchant
from simulator.outcomes.ground_truth import LatentRecoverabilityFunction
from simulator.payments.generator import GeneratedBatchResult, PaymentGenerator

GENERATOR_VERSION = "simulator-v2.0"


def build_simulator(
    seed: int = 42,
    scenario_config: dict[str, Any] | None = None,
    customer_count: int = 2000,
    start_time: datetime | None = None,
) -> tuple[
    PaymentGenerator,
    list[SimulatedMerchant],
    list[SimulatedCustomer],
    SimulationManifestData,
]:
    id_gen = DeterministicIdGenerator(seed)
    rng = SimRng(seed)
    clock = SimClock(start_time or datetime(2026, 8, 20, 9, 0, 0, tzinfo=timezone.utc))

    # 1. Generate Merchants & Customers
    merchant_gen = MerchantGenerator(id_gen, rng, clock.get_time())
    merchants = merchant_gen.generate_merchants()

    customer_gen = CustomerGenerator(id_gen, rng, clock.get_time(), opt_out_baseline_rate=0.04)
    customers = customer_gen.generate_customers(customer_count, merchants)

    # 2. Configure Scenarios
    sc_conf = scenario_config or {}
    scenarios = [
        NormalFailureScenario(baseline_failure_rate=sc_conf.get("normal_rate", 0.03)),
        BankDegradationScenario(
            target_bank=sc_conf.get("degradation_bank", "HDFC"),
            spike_rate=sc_conf.get("degradation_rate", 0.18),
            window_start=clock.get_time(),
            window_duration_minutes=sc_conf.get("degradation_minutes", 180),
        ),
        MultiRailOutageScenario(
            affected_banks=sc_conf.get("outage_banks", ["ICICI", "SBI"]),
            outage_failure_rate=sc_conf.get("outage_rate", 0.35),
            window_start=clock.get_time(),
            window_duration_minutes=sc_conf.get("outage_minutes", 90),
        ),
        TemporaryTimeoutScenario(failure_rate=sc_conf.get("timeout_rate", 0.08)),
        PermanentFailureScenario(failure_rate=sc_conf.get("permanent_rate", 0.06)),
        CustomerRepeatFailureScenario(failure_rate=sc_conf.get("customer_repeat_rate", 0.12)),
    ]

    # 3. Observation Noise & Latent Ground Truth
    noise_pipeline = ObservationNoisePipeline(rng, ambiguity_rate=0.12)
    latent_function = LatentRecoverabilityFunction(rng)

    manifest_data = SimulationManifestData(
        simulation_id=id_gen.simulation_id(),
        seed=seed,
        generator_version=GENERATOR_VERSION,
        scenario_config=sc_conf,
        latent_function_version=LatentRecoverabilityFunction.VERSION,
        total_payments=0,
        created_at=clock.get_time(),
    )

    generator = PaymentGenerator(
        id_gen=id_gen,
        rng=rng,
        clock=clock,
        merchants=merchants,
        customers=customers,
        scenarios=scenarios,
        noise_pipeline=noise_pipeline,
        latent_function=latent_function,
    )

    return generator, merchants, customers, manifest_data


def save_to_database(
    manifest_data: SimulationManifestData,
    merchants: list[SimulatedMerchant],
    customers: list[SimulatedCustomer],
    batch_result: GeneratedBatchResult,
    batch_size: int = 1000,
) -> None:
    """
    Persist generated simulator batch to Postgres via SQLAlchemy.
    Uses batch bulk inserts for high throughput.
    """
    engine = get_sync_engine()
    with Session(engine) as session:
        # 1. Upsert Merchants
        for m in merchants:
            existing = session.get(Merchant, m.merchant_id)
            if not existing:
                session.add(
                    Merchant(
                        merchant_id=m.merchant_id,
                        name=m.name,
                        created_at=m.created_at,
                    )
                )
        session.flush()

        # 2. Upsert Customers
        for c in customers:
            existing = session.get(Customer, c.customer_id)
            if not existing:
                session.add(
                    Customer(
                        customer_id=c.customer_id,
                        merchant_id=c.merchant_id,
                        is_returning=c.is_returning,
                        lifetime_value_paise=c.lifetime_value_paise,
                        opted_out_at=c.opted_out_at,
                        created_at=c.created_at,
                    )
                )
        session.flush()

        # 3. Create Simulation Manifest
        existing_manifest = session.get(SimulationManifest, manifest_data.simulation_id)
        if not existing_manifest:
            session.add(
                SimulationManifest(
                    simulation_id=manifest_data.simulation_id,
                    seed=manifest_data.seed,
                    generator_version=manifest_data.generator_version,
                    scenario_config=manifest_data.scenario_config,
                    latent_function_version=manifest_data.latent_function_version,
                    total_payments=len(batch_result.payments),
                    created_at=manifest_data.created_at,
                )
            )
        else:
            existing_manifest.total_payments = len(batch_result.payments)
        session.flush()

        # 4. Insert Payments in Chunks
        for i in range(0, len(batch_result.payments), batch_size):
            chunk = batch_result.payments[i : i + batch_size]
            payment_objs = [
                Payment(
                    payment_id=p.payment_id,
                    merchant_id=p.merchant_id,
                    customer_id=p.customer_id,
                    amount_paise=p.amount_paise,
                    method=p.method,
                    bank=p.bank,
                    status=p.status,
                    failure_code=p.failure_code,
                    failure_class=p.failure_class,
                    is_synthetic=p.is_synthetic,
                    ground_truth_recoverable=p.ground_truth_recoverable,
                    created_at=p.created_at,
                    failed_at=p.failed_at,
                )
                for p in chunk
            ]
            session.bulk_save_objects(payment_objs)
            session.flush()

        # 5. Insert Events in Chunks
        for i in range(0, len(batch_result.events), batch_size):
            chunk = batch_result.events[i : i + batch_size]
            event_objs = [
                Event(
                    event_id=e.event_id,
                    payment_id=e.payment_id,
                    event_type=e.event_type,
                    payload=e.payload,
                    occurred_at=e.occurred_at,
                )
                for e in chunk
            ]
            session.bulk_save_objects(event_objs)
            session.flush()

        # 6. Insert Simulator Latent State in Chunks
        for i in range(0, len(batch_result.latent_records), batch_size):
            chunk = batch_result.latent_records[i : i + batch_size]
            latent_objs = [
                SimulatorLatentState(
                    latent_id=l.latent_id,
                    simulation_id=l.simulation_id,
                    payment_id=l.payment_id,
                    customer_patience_score=l.customer_patience_score,
                    bank_latent_health=l.bank_latent_health,
                    latent_network_noise=l.latent_network_noise,
                    latent_customer_propensity=l.latent_customer_propensity,
                    true_recovery_prob_bps=l.true_recovery_prob_bps,
                    true_failure_type=l.true_failure_type,
                    created_at=l.created_at,
                )
                for l in chunk
            ]
            session.bulk_save_objects(latent_objs)
            session.flush()

        session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="RecoveryOS Payment Simulation CLI")
    parser.add_argument("--n", type=int, default=10000, help="Number of payments to generate")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument(
        "--scenario-weights",
        type=str,
        default="{}",
        help="JSON string of scenario configuration overrides",
    )
    parser.add_argument(
        "--output",
        choices=["db", "dry-run"],
        default="db",
        help="Output destination: db or dry-run",
    )
    parser.add_argument(
        "--customers", type=int, default=2000, help="Number of customers to generate"
    )

    args = parser.parse_args()

    try:
        scenario_config = json.loads(args.scenario_weights)
    except Exception as e:
        print(f"Error parsing --scenario-weights: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[*] Starting RecoveryOS Simulation (n={args.n}, seed={args.seed}, output={args.output})...")
    start_time = time.time()

    generator, merchants, customers, manifest_data = build_simulator(
        seed=args.seed,
        scenario_config=scenario_config,
        customer_count=args.customers,
    )

    gen_start = time.time()
    batch_result = generator.generate_batch(args.n, manifest_data.simulation_id)
    gen_duration = time.time() - gen_start

    failed_payments = [p for p in batch_result.payments if p.status == "failed"]
    success_payments = [p for p in batch_result.payments if p.status == "success"]
    recoverable_failed = [p for p in failed_payments if p.ground_truth_recoverable is True]

    print(f"[+] In-Memory Generation Complete in {gen_duration:.2f}s:")
    print(f"    - Total Payments:     {len(batch_result.payments):,}")
    print(f"    - Successful:         {len(success_payments):,} ({len(success_payments)/len(batch_result.payments)*100:.1f}%)")
    print(f"    - Failed:             {len(failed_payments):,} ({len(failed_payments)/len(batch_result.payments)*100:.1f}%)")
    if failed_payments:
        print(f"    - Recoverable Failed: {len(recoverable_failed):,} ({len(recoverable_failed)/len(failed_payments)*100:.1f}% of failures)")
    print(f"    - Total Events:       {len(batch_result.events):,}")
    print(f"    - Latent Records:     {len(batch_result.latent_records):,}")

    if args.output == "db":
        print(f"[*] Persisting records to PostgreSQL database...")
        db_start = time.time()
        save_to_database(manifest_data, merchants, customers, batch_result)
        db_duration = time.time() - db_start
        print(f"[+] Database Insertion Complete in {db_duration:.2f}s.")

    total_duration = time.time() - start_time
    print(f"[✓] Simulation Run Completed in {total_duration:.2f}s.")


if __name__ == "__main__":
    main()
