"""
gaps.md sec:B.4's own named test, never written anywhere before this file:
SUM(actual_recovery_paise) computed by real Postgres must exactly equal a
Python-side sum of the same rows -- zero tolerance, not approx-equal. This
is the direct proof that recovery_ledger's integer-paise columns (BigInteger
throughout, recoveryos/models.py's RecoveryLedger) never accumulate the
representation drift a float/NUMERIC-adjacent type would under a large
SUM() -- the exact number this codebase's headline "incremental recovered
revenue" metric depends on.

tests/integration/test_reconciled_metrics.py's own
test_reconciled_gauges_match_the_real_ledger_sum compares a Prometheus gauge
to a single-row SQL SUM() -- a different, narrower claim (does the /metrics
endpoint reflect the DB, not "does SQL SUM() equal Python sum() across many
rows"). This file is the one gaps.md actually asked for.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import to_async_url

ROW_COUNT = 500


@pytest.mark.asyncio
async def test_ledger_sum_matches_sum_of_individual_rows_exactly(migrated_db):
    merchant_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    rng = random.Random(20260902)

    payment_rows = []
    ledger_rows = []
    python_sum = 0
    now = datetime.now(UTC)
    for i in range(ROW_COUNT):
        payment_id = str(uuid.uuid4())
        # A realistic paise range (₹1 to ₹50,000), including some zeros (an
        # attempt that recovered nothing) -- not a narrow "nice" range that
        # would coincidentally hide rounding behavior.
        amount = rng.choice([0, *range(100, 5_000_000)])
        python_sum += amount
        payment_rows.append(
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "amount": max(amount, 1),
                "ts": now - timedelta(minutes=i),
            }
        )
        ledger_rows.append(
            {
                "lid": str(uuid.uuid4()),
                "pid": payment_id,
                "risk": max(amount, 1),
                "expected": amount,
                "actual": amount,
            }
        )

    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO merchants (merchant_id, name) VALUES (:mid, :name) "
                "ON CONFLICT (merchant_id) DO NOTHING"
            ),
            {"mid": merchant_id, "name": "ledger-sum-test"},
        )
        await conn.execute(
            text(
                "INSERT INTO customers (customer_id, merchant_id) VALUES (:cid, :mid) "
                "ON CONFLICT (customer_id) DO NOTHING"
            ),
            {"cid": customer_id, "mid": merchant_id},
        )
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, :amount, 'upi', 'HDFC', 'recovered', true, :ts, :ts)"
            ),
            payment_rows,
        )
        await conn.execute(
            text(
                "INSERT INTO recovery_ledger (ledger_id, payment_id, revenue_at_risk_paise, "
                "expected_recovery_paise, actual_recovery_paise) "
                "VALUES (:lid, :pid, :risk, :expected, :actual)"
            ),
            ledger_rows,
        )

        sql_sum = (
            await conn.execute(
                text(
                    "SELECT COALESCE(SUM(actual_recovery_paise), 0) FROM recovery_ledger "
                    "WHERE payment_id = ANY(:pids)"
                ),
                {"pids": [r["pid"] for r in payment_rows]},
            )
        ).scalar_one()
    await engine.dispose()

    assert sql_sum == python_sum, (
        f"SQL SUM() ({sql_sum}) must exactly equal the Python-side sum of the same "
        f"{ROW_COUNT} rows ({python_sum}) -- zero tolerance, any difference is a real "
        f"precision bug, not rounding noise"
    )
