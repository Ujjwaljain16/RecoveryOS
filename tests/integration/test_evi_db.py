"""
Integration tests for services/recovery_engine/evi.py:get_action_cost —
gaps.md §A.2. Real Postgres, proving the DB resolution path (not a
hardcoded fallback).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from services.recovery_engine.evi import calculate_evi, get_action_cost
from tests.integration.conftest import to_async_url


@pytest.mark.asyncio
async def test_evi_uses_db_action_cost_not_hardcoded_constant(migrated_db):
    """
    Change a cost row in the DB, assert EVI output for a fixed payment
    changes correspondingly — proving no hardcoded fallback exists in the
    code path.
    """
    engine = create_async_engine(to_async_url(migrated_db))
    async with AsyncSession(engine) as session:
        original = await get_action_cost(session, merchant_id=None, action_type="REMINDER")
        original_evi = calculate_evi(8200, 100_000, original.cost_paise, original.friction_base_paise, 0)

    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE action_costs SET cost_paise = cost_paise + 1000 WHERE merchant_id IS NULL AND action_type = 'REMINDER'")
        )

    async with AsyncSession(engine) as session:
        updated = await get_action_cost(session, merchant_id=None, action_type="REMINDER")
        updated_evi = calculate_evi(8200, 100_000, updated.cost_paise, updated.friction_base_paise, 0)

    assert updated.cost_paise == original.cost_paise + 1000
    assert updated_evi == original_evi - 1000

    await engine.dispose()


@pytest.mark.asyncio
async def test_merchant_specific_cost_overrides_platform_default(migrated_db):
    engine = create_async_engine(to_async_url(migrated_db))
    merchant_id = str(uuid.uuid4())

    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO merchants (merchant_id, name) VALUES (:mid, :name)"),
            {"mid": merchant_id, "name": "evi-test-merchant"},
        )
        await conn.execute(
            text(
                "INSERT INTO action_costs (merchant_id, action_type, cost_paise, friction_base_paise) "
                "VALUES (:mid, 'ESCALATE', 99999, 1)"
            ),
            {"mid": merchant_id},
        )

    async with AsyncSession(engine) as session:
        merchant_cost = await get_action_cost(session, merchant_id=merchant_id, action_type="ESCALATE")
        platform_cost = await get_action_cost(session, merchant_id=None, action_type="ESCALATE")

    assert merchant_cost.cost_paise == 99_999
    assert platform_cost.cost_paise != 99_999  # platform default (15000 seed) untouched

    await engine.dispose()


@pytest.mark.asyncio
async def test_missing_merchant_cost_falls_back_to_platform_default_not_error(migrated_db):
    engine = create_async_engine(to_async_url(migrated_db))
    merchant_id = str(uuid.uuid4())

    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO merchants (merchant_id, name) VALUES (:mid, :name)"),
            {"mid": merchant_id, "name": "no-override-merchant"},
        )
        # deliberately NOT inserting a merchant-specific action_costs row

    async with AsyncSession(engine) as session:
        cost = await get_action_cost(session, merchant_id=merchant_id, action_type="RETRY_NOW")

    # Platform default seed: RETRY_NOW cost=0, friction_base=10 (migrations/0001)
    assert cost.cost_paise == 0
    assert cost.friction_base_paise == 10

    await engine.dispose()
