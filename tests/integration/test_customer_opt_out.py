"""
POST /v1/customers/{customer_id}/opt-out (gaps.md sec:A.1) -- real Postgres,
real merchant-scoped auth, real end-to-end policy effect. Closes gaps.md's
own three named tests for this section, none of which existed anywhere in
the repo before this file (the endpoint itself didn't exist either --
OptOutRule was implemented and unit-tested from day one, but nothing ever
called it via a real customer action).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from apps.api.dependencies.auth import generate_api_key
from tests.integration.conftest import seed_merchant_with_api_key, to_async_url


async def _seed_merchant(migrated_db: str, name: str = "optout-test") -> tuple[str, str]:
    merchant_id = str(uuid.uuid4())
    raw_key = generate_api_key()
    await seed_merchant_with_api_key(migrated_db, merchant_id, name, raw_key)
    return merchant_id, raw_key


async def _seed_customer(migrated_db: str, merchant_id: str) -> str:
    customer_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO customers (customer_id, merchant_id, is_returning, "
                "lifetime_value_paise) VALUES (:cid, :mid, false, 0)"
            ),
            {"cid": customer_id, "mid": merchant_id},
        )
    await engine.dispose()
    return customer_id


@pytest.mark.asyncio
async def test_opt_out_endpoint_is_idempotent(async_client, migrated_db):
    merchant_id, api_key = await _seed_merchant(migrated_db)
    customer_id = await _seed_customer(migrated_db, merchant_id)

    resp1 = await async_client.post(
        f"/v1/customers/{customer_id}/opt-out",
        json={"reason": "too many texts", "channel": "sms"},
        headers={"X-API-Key": api_key},
    )
    assert resp1.status_code == 200
    first_opted_out_at = resp1.json()["opted_out_at"]
    assert resp1.json()["customer_id"] == customer_id

    # Re-calling with DIFFERENT payload fields must not overwrite the
    # original timestamp, and must not error.
    resp2 = await async_client.post(
        f"/v1/customers/{customer_id}/opt-out",
        json={"channel": "email"},
        headers={"X-API-Key": api_key},
    )
    assert resp2.status_code == 200
    assert resp2.json()["opted_out_at"] == first_opted_out_at

    # Exactly one audit_log row -- the idempotent re-call must not write a
    # second one for what is, from the customer's perspective, one event.
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM audit_log WHERE customer_id = :cid"),
                {"cid": customer_id},
            )
        ).scalar_one()
    await engine.dispose()
    assert count == 1


@pytest.mark.asyncio
async def test_opt_out_404s_for_a_different_merchants_customer(async_client, migrated_db):
    merchant_a, _ = await _seed_merchant(migrated_db, "optout-a")
    _merchant_b, api_key_b = await _seed_merchant(migrated_db, "optout-b")
    customer_id = await _seed_customer(migrated_db, merchant_a)

    resp = await async_client.post(
        f"/v1/customers/{customer_id}/opt-out", json={}, headers={"X-API-Key": api_key_b}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_opted_out_customer_never_receives_further_intervention(async_client, migrated_db):
    """
    End-to-end, real pipeline: opt a customer out via the REAL HTTP
    endpoint mid-recovery-workflow, then run the REAL decision orchestrator
    (services.recovery_engine.orchestrator.decide_and_persist -- the exact
    function services/pipeline/consumer.py calls in production) for their
    failed payment, and assert it returns BLOCK via OptOutRule specifically
    -- not merely "some rule blocked it."
    """
    from services.recovery_engine.orchestrator import decide_and_persist

    merchant_id, api_key = await _seed_merchant(migrated_db, "optout-e2e")
    customer_id = await _seed_customer(migrated_db, merchant_id)
    payment_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, "
                "failed_at) VALUES (:pid, :mid, :cid, 50000, 'upi', 'HDFC', 'failed', 'TIMEOUT', "
                "'TEMPORARY', true, now(), now())"
            ),
            {"pid": payment_id, "mid": merchant_id, "cid": customer_id},
        )
    await engine.dispose()

    resp = await async_client.post(
        f"/v1/customers/{customer_id}/opt-out",
        json={"channel": "support_call", "reason": "asked to stop"},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200

    # redis_client=None is safe here: a BLOCK verdict never reaches
    # decide_and_persist's enqueue branch (gated on verdict == "ALLOW").
    result = await decide_and_persist(payment_id, redis_client=None)
    assert result["verdict"] == "BLOCK"
    assert result["blocking_rule"] == "OptOutRule"


def test_simulator_and_live_endpoint_share_same_code_path():
    """
    gaps.md sec:A.1's named test: the simulator's opt-out writes
    (simulator/run.py::save_to_database) must hit the SAME function the
    live endpoint (apps/api/routers/customers.py) uses, not an
    independently-maintained DB-write shortcut that could silently drift
    from it. Strongest possible proof: object identity, not "both files
    mention opt-out somewhere" or matching source text.
    """
    import apps.api.routers.customers as customers_router
    import simulator.run as sim_run
    from services.customer_engine.opt_out import apply_customer_opt_out

    assert customers_router.apply_customer_opt_out is apply_customer_opt_out
    assert sim_run.apply_customer_opt_out is apply_customer_opt_out
