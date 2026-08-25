"""
inference_role permission tests — gaps.md §B.1. Mirrors
test_schema_and_roles.py's diagnoser_role tests exactly, for the propensity
model's restricted read path (migrations/0008_inference_role.py).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text


def test_inference_role_cannot_select_ground_truth_recoverable(migrated_db):
    engine = create_engine(migrated_db)
    with engine.connect() as conn:
        conn.execute(
            text("INSERT INTO merchants (merchant_id, name) VALUES (gen_random_uuid(), 'test_merchant')")
        )
        conn.execute(
            text(
                "INSERT INTO customers (customer_id, merchant_id) "
                "SELECT gen_random_uuid(), merchant_id FROM merchants LIMIT 1"
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO payments (
                    payment_id, merchant_id, customer_id,
                    amount_paise, method, status, ground_truth_recoverable
                )
                SELECT gen_random_uuid(), m.merchant_id, c.customer_id,
                       89900, 'upi', 'failed', true
                FROM merchants m CROSS JOIN customers c LIMIT 1
                """
            )
        )
        conn.commit()

        conn.execute(text("SET ROLE inference_role"))
        with pytest.raises(Exception) as exc_info:
            conn.execute(text("SELECT ground_truth_recoverable FROM payments LIMIT 1"))

        err = str(exc_info.value).lower()
        assert any(p in err for p in ["permission denied", "denied", "privilege", "column"])

    engine.dispose()


def test_inference_role_can_read_safe_payment_columns(migrated_db):
    engine = create_engine(migrated_db)
    with engine.connect() as conn:
        conn.execute(text("SET ROLE inference_role"))
        result = conn.execute(
            text("SELECT payment_id, amount_paise, method, bank, failure_code FROM payments LIMIT 1")
        )
        result.fetchall()  # must not raise

    engine.dispose()


def test_inference_role_has_no_write_access(migrated_db):
    engine = create_engine(migrated_db)
    with engine.connect() as conn:
        conn.execute(text("SET ROLE inference_role"))
        with pytest.raises(Exception) as exc_info:
            conn.execute(
                text(
                    "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                    "method, status) VALUES (gen_random_uuid(), gen_random_uuid(), "
                    "gen_random_uuid(), 100, 'upi', 'created')"
                )
            )
        assert "permission denied" in str(exc_info.value).lower()

    engine.dispose()


def test_inference_role_zero_grants_on_simulator_latent_state(migrated_db):
    engine = create_engine(migrated_db)
    with engine.connect() as conn:
        conn.execute(text("SET ROLE inference_role"))
        with pytest.raises(Exception) as exc_info:
            conn.execute(text("SELECT * FROM simulator_latent_state LIMIT 1"))
        assert "permission denied" in str(exc_info.value).lower()

    engine.dispose()
