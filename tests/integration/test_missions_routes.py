"""
GET /v1/missions/active and GET /v1/payments/{payment_id}/mission --
Phase 12/13's mission machinery finally exposed via a real API, not just
queryable by SQL. Same real-auth pattern as tests/integration/test_stub_routes.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from apps.api.dependencies.auth import generate_api_key
from tests.integration.conftest import seed_merchant_with_api_key, to_async_url


async def _seed_merchant(migrated_db: str, name: str = "missions-route-test") -> tuple[str, str]:
    merchant_id = str(uuid.uuid4())
    raw_key = generate_api_key()
    await seed_merchant_with_api_key(migrated_db, merchant_id, name, raw_key)
    return merchant_id, raw_key


async def _seed_payment(migrated_db: str, merchant_id: str, amount_paise: int = 100_000) -> str:
    customer_id = str(uuid.uuid4())
    payment_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO customers (customer_id, merchant_id, is_returning, "
                "lifetime_value_paise) VALUES (:cid, :mid, false, 0)"
            ),
            {"cid": customer_id, "mid": merchant_id},
        )
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, :amount, 'upi', 'HDFC', 'failed', 'TIMEOUT', 'TEMPORARY', "
                "true, now(), now())"
            ),
            {"pid": payment_id, "mid": merchant_id, "cid": customer_id, "amount": amount_paise},
        )
    await engine.dispose()
    return payment_id


async def _seed_mission(
    migrated_db: str, payment_id: str, *, state: str, amount_paise: int = 100_000
) -> str:
    mission_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO recovery_missions (mission_id, payment_id, state, objective, "
                "max_investigation_rounds, max_attempts, max_mission_duration_seconds, "
                "max_money_exposure_paise, current_round, current_attempt, started_at, expires_at) "
                "VALUES (:mid, :pid, :state, 'test objective', 3, 3, 604800, :amount, 0, 1, "
                ":now, :expires)"
            ),
            {
                "mid": mission_id,
                "pid": payment_id,
                "state": state,
                "amount": amount_paise,
                "now": now,
                "expires": now + timedelta(days=7),
            },
        )
        for seq, (event_type, actor, ev_state) in enumerate(
            [
                ("MISSION_CREATED", "system", "INVESTIGATING"),
                ("HYPOTHESIS_UPDATED", "ai", "INVESTIGATING"),
                ("POLICY_AUTHORIZED", "policy_engine", "EXECUTING"),
            ],
            start=1,
        ):
            await conn.execute(
                text(
                    "INSERT INTO mission_events (event_id, mission_id, sequence_number, state, "
                    "event_type, actor, payload) "
                    "VALUES (gen_random_uuid(), :mid, :seq, :state, :event_type, :actor, '{}'::jsonb)"
                ),
                {
                    "mid": mission_id,
                    "seq": seq,
                    "state": ev_state,
                    "event_type": event_type,
                    "actor": actor,
                },
            )
    await engine.dispose()
    return mission_id


@pytest.mark.asyncio
async def test_active_missions_empty_for_a_fresh_merchant(async_client, migrated_db):
    _merchant_id, api_key = await _seed_merchant(migrated_db)
    resp = await async_client.get("/v1/missions/active", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    assert resp.json() == {"missions": []}


@pytest.mark.asyncio
async def test_active_missions_lists_non_terminal_and_excludes_terminal(async_client, migrated_db):
    merchant_id, api_key = await _seed_merchant(migrated_db)
    active_payment_id = await _seed_payment(migrated_db, merchant_id)
    await _seed_mission(migrated_db, active_payment_id, state="EXECUTING")
    recovered_payment_id = await _seed_payment(migrated_db, merchant_id)
    await _seed_mission(migrated_db, recovered_payment_id, state="RECOVERED")

    resp = await async_client.get("/v1/missions/active", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    body = resp.json()
    payment_ids = {m["payment_id"] for m in body["missions"]}
    assert active_payment_id in payment_ids
    assert recovered_payment_id not in payment_ids
    entry = next(m for m in body["missions"] if m["payment_id"] == active_payment_id)
    assert entry["state"] == "EXECUTING"
    assert entry["amount_paise"] == 100_000


@pytest.mark.asyncio
async def test_active_missions_scoped_to_the_authenticated_merchant(async_client, migrated_db):
    merchant_a, api_key_a = await _seed_merchant(migrated_db, name="merchant-a")
    merchant_b, api_key_b = await _seed_merchant(migrated_db, name="merchant-b")
    payment_a = await _seed_payment(migrated_db, merchant_a)
    await _seed_mission(migrated_db, payment_a, state="EXECUTING")

    resp_b = await async_client.get("/v1/missions/active", headers={"X-API-Key": api_key_b})
    assert resp_b.json() == {"missions": []}

    resp_a = await async_client.get("/v1/missions/active", headers={"X-API-Key": api_key_a})
    assert len(resp_a.json()["missions"]) == 1


@pytest.mark.asyncio
async def test_payment_mission_404s_when_no_mission_exists(async_client, migrated_db):
    merchant_id, api_key = await _seed_merchant(migrated_db)
    payment_id = await _seed_payment(migrated_db, merchant_id)
    resp = await async_client.get(
        f"/v1/payments/{payment_id}/mission", headers={"X-API-Key": api_key}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_payment_mission_404s_for_a_different_merchants_payment(async_client, migrated_db):
    merchant_a, api_key_a = await _seed_merchant(migrated_db, name="merchant-a-2")
    _merchant_b, api_key_b = await _seed_merchant(migrated_db, name="merchant-b-2")
    payment_id = await _seed_payment(migrated_db, merchant_a)
    await _seed_mission(migrated_db, payment_id, state="EXECUTING")

    resp = await async_client.get(
        f"/v1/payments/{payment_id}/mission", headers={"X-API-Key": api_key_b}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_payment_mission_returns_the_full_ordered_event_trace(async_client, migrated_db):
    merchant_id, api_key = await _seed_merchant(migrated_db)
    payment_id = await _seed_payment(migrated_db, merchant_id, amount_paise=842_000)
    mission_id = await _seed_mission(
        migrated_db, payment_id, state="EXECUTING", amount_paise=842_000
    )

    resp = await async_client.get(
        f"/v1/payments/{payment_id}/mission", headers={"X-API-Key": api_key}
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["payment_id"] == payment_id
    assert body["mission"]["mission_id"] == mission_id
    assert body["mission"]["state"] == "EXECUTING"
    assert body["mission"]["max_money_exposure_paise"] == 842_000

    event_types = [e["event_type"] for e in body["events"]]
    assert event_types == ["MISSION_CREATED", "HYPOTHESIS_UPDATED", "POLICY_AUTHORIZED"]
    sequence_numbers = [e["sequence_number"] for e in body["events"]]
    assert sequence_numbers == [1, 2, 3]
