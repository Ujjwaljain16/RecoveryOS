"""
Task I2, the strong version: a real Razorpay outage (mocked at the HTTP
layer only) must produce a genuine SimulatorAdapter resolution against a
real simulator_latent_state row in real Postgres -- not just "some spy
object got called," proving the fallback actually resolves outcomes, not
merely delegates to something that could itself be a no-op.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import integrations.razorpay.adapter as adapter_module
from integrations.razorpay.adapter import RazorpayTestAdapter
from tests.integration.conftest import seed_merchant_and_customer, to_async_url


async def _seed_latent_state(migrated_db: str, payment_id: str, true_recovery_prob_bps: int) -> None:
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    await seed_merchant_and_customer(migrated_db, merchant_id, customer_id)

    engine = create_async_engine(to_async_url(migrated_db))
    simulation_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, 500000, 'upi', 'HDFC', 'failed', 'TIMEOUT', true, now(), now())"
            ),
            {"pid": payment_id, "mid": merchant_id, "cid": customer_id},
        )
        await conn.execute(
            text(
                "INSERT INTO simulator_manifests (simulation_id, seed, generator_version, "
                "scenario_config, latent_function_version, total_payments) "
                "VALUES (:sim_id, 1, 'test', '{}'::jsonb, 'test-v1', 1)"
            ),
            {"sim_id": simulation_id},
        )
        await conn.execute(
            text(
                "INSERT INTO simulator_latent_state (latent_id, simulation_id, payment_id, "
                "customer_patience_score, bank_latent_health, latent_network_noise, "
                "latent_customer_propensity, true_recovery_prob_bps, true_failure_type) "
                "VALUES (:lid, :sim_id, :pid, 0.8, 0.9, 0.1, 0.2, :prob, 'TEMPORARY_GATEWAY_TIMEOUT')"
            ),
            {"lid": str(uuid.uuid4()), "sim_id": simulation_id, "pid": payment_id, "prob": true_recovery_prob_bps},
        )
    await engine.dispose()


def test_razorpay_outage_falls_back_to_simulator(migrated_db, monkeypatch, caplog):
    """
    Force a genuine httpx failure (ConnectError), then confirm the returned
    ProviderResult is a REAL SUCCESS/FAILED resolution against a real
    simulator_latent_state row -- not a bare PENDING (the pre-fix behavior)
    and not merely "some object was called" (a spy could pass that check
    even if the fallback did nothing real).
    """
    import asyncio

    payment_id = str(uuid.uuid4())
    # true_recovery_prob_bps = 10000 -> deterministically SUCCESS, so the
    # test isn't relying on getting lucky with the random coin flip.
    asyncio.run(_seed_latent_state(migrated_db, payment_id, 10_000))

    class _DeadClient:
        def post(self, url, **kwargs):
            raise httpx.ConnectError("simulated Razorpay outage", request=None)

    monkeypatch.setattr(adapter_module, "_get_shared_http_client", lambda: _DeadClient())

    from sqlalchemy import create_engine

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    adapter = RazorpayTestAdapter()

    with caplog.at_level("WARNING"):
        with sync_engine.connect() as conn:
            result = adapter.retry(conn, payment_id, 500_000, 1)

    assert result.outcome == "SUCCESS", (
        "with true_recovery_prob_bps=10000 the fallback's SimulatorAdapter resolution "
        "must deterministically succeed -- a bare/fake PENDING would fail this"
    )
    assert result.recovered_amount_paise == 500_000
    assert result.provider_ref is not None and result.provider_ref.startswith("sim_"), (
        "provider_ref must show this came from SimulatorAdapter's real ref format, "
        "not a Razorpay order id or a null placeholder"
    )
    assert any("RAZORPAY_OUTAGE_FALLBACK" in r.message for r in caplog.records)

    sync_engine.dispose()
