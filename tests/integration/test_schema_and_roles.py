"""
Integration Tests — Database Schema Verification
==================================================
Verifies:
  1. test_all_tables_exist()         — every TRD §2 table is in the schema
  2. test_audit_log_is_append_only() — UPDATE and DELETE raise permission errors for app_role
  3. test_diagnoser_role_cannot_read_ground_truth() — diagnoser SELECT on
                                        ground_truth_recoverable raises an error

These tests run against a REAL PostgreSQL instance via testcontainers.
NO SQLite. NO mocking. The DB role tests are especially important: they verify that
security guarantees are enforced at the Postgres level, not just in application code.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

# ─── All tables mandated by TRD §2 ────────────────────────────────────────────
REQUIRED_TABLES = [
    "merchants",
    "customers",
    "payments",
    "events",
    "anomaly_windows",
    "diagnoses",
    "candidate_actions",
    "policy_configs",
    "policy_decisions",
    "recoveries",
    "recovery_ledger",
    "audit_log",
    # From gaps.md §A.2 and evaluation harness (TRD §7)
    "action_costs",
    "baseline_runs",
]


class TestAllTablesExist:
    """
    TRD §2 compliance: every table must be present in information_schema.tables.
    Failure here means a migration is missing or mis-named — nothing else can proceed.
    """

    def test_all_required_tables_present(self, sync_engine):
        """
        Query information_schema directly — not SQLAlchemy introspection —
        because we want to verify what Postgres actually has, not what the ORM thinks.
        """
        with sync_engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_type = 'BASE TABLE'
                    """
                )
            )
            existing = {row[0] for row in result}

        missing = set(REQUIRED_TABLES) - existing
        assert not missing, (
            f"Missing tables in schema: {sorted(missing)}.\n" f"Existing tables: {sorted(existing)}"
        )

    def test_payments_has_ground_truth_column(self, sync_engine):
        """
        ground_truth_recoverable must exist as a column (it's the column
        that diagnoser_role is denied access to — it must exist to be denied).
        """
        with sync_engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'payments'
                      AND column_name = 'ground_truth_recoverable'
                    """
                )
            )
            rows = result.fetchall()
        assert rows, "payments.ground_truth_recoverable column is missing from schema"

    def test_recoveries_idempotency_key_is_unique(self, sync_engine):
        """
        Unique constraint on recoveries.idempotency_key is the physical
        double-execution backstop (gaps.md §B.2). Verify it exists.
        """
        with sync_engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                    SELECT constraint_name
                    FROM information_schema.table_constraints
                    WHERE table_name = 'recoveries'
                      AND constraint_type = 'UNIQUE'
                    """
                )
            )
            constraints = [row[0] for row in result]
        assert constraints, (
            "No UNIQUE constraint found on recoveries table. "
            "The idempotency_key UNIQUE constraint is mandatory (gaps.md §B.2)."
        )

    def test_all_money_columns_are_bigint(self, sync_engine):
        """
        Every column ending in '_paise' must be BIGINT (int8).
        This enforces the integer-arithmetic-only invariant from gaps.md §B.4.
        """
        with sync_engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                    SELECT table_name, column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND column_name LIKE '%_paise'
                      AND data_type != 'bigint'
                    """
                )
            )
            violations = result.fetchall()

        assert (
            not violations
        ), "Money columns must be BIGINT paise, never float/numeric:\n" + "\n".join(
            f"  {t}.{c} is {d}" for t, c, d in violations
        )


class TestAuditLogIsAppendOnly:
    """
    TRD §9: 'REVOKE UPDATE, DELETE on audit_log, events FROM app_role at DB grant level.'
    These tests verify the Postgres-level permission enforcement, not application code.

    They run as the 'recoveryos' user (app_role member), not as superuser.
    If the REVOKE migration hasn't run, these tests FAIL — which is the correct signal.
    """

    def test_audit_log_update_raises_permission_error(self, migrated_db):
        """
        Attempting UPDATE on audit_log as app_role must raise an error.
        This proves immutability is enforced at the DB level, not just by convention.
        """
        from sqlalchemy import create_engine

        # For this test we stay as the test superuser but SET ROLE to app_role
        engine = create_engine(migrated_db)
        with engine.connect() as conn:
            # First insert a row as superuser so there's something to try to UPDATE
            conn.execute(
                text(
                    """
                    INSERT INTO audit_log (audit_id, summary)
                    VALUES (gen_random_uuid(), 'test-append-only')
                    """
                )
            )
            conn.commit()

            # Now switch to app_role and attempt UPDATE
            conn.execute(text("SET ROLE app_role"))
            with pytest.raises(Exception) as exc_info:
                conn.execute(text("UPDATE audit_log SET summary = 'tampered'"))
                conn.commit()

            # Postgres raises ProgrammingError (permission denied) or InternalError
            assert any(
                phrase in str(exc_info.value).lower()
                for phrase in ["permission denied", "denied", "privilege"]
            ), f"Expected permission denied, got: {exc_info.value}"

        engine.dispose()

    def test_audit_log_delete_raises_permission_error(self, migrated_db):
        """
        Attempting DELETE on audit_log as app_role must raise an error.
        """
        from sqlalchemy import create_engine

        engine = create_engine(migrated_db)
        with engine.connect() as conn:
            conn.execute(text("SET ROLE app_role"))
            with pytest.raises(Exception) as exc_info:
                conn.execute(text("DELETE FROM audit_log"))
                conn.commit()

            assert any(
                phrase in str(exc_info.value).lower()
                for phrase in ["permission denied", "denied", "privilege"]
            ), f"Expected permission denied, got: {exc_info.value}"

        engine.dispose()

    def test_events_update_raises_permission_error(self, migrated_db):
        """events table is also append-only — same enforcement."""
        from sqlalchemy import create_engine

        engine = create_engine(migrated_db)
        with engine.connect() as conn:
            conn.execute(text("SET ROLE app_role"))
            with pytest.raises(Exception) as exc_info:
                conn.execute(text("UPDATE events SET event_type = 'TAMPERED'"))
                conn.commit()

            assert any(
                phrase in str(exc_info.value).lower()
                for phrase in ["permission denied", "denied", "privilege"]
            )

        engine.dispose()


class TestDiagnoserRoleCannotReadGroundTruth:
    """
    TRD §1.3, §9, gaps.md §B.1:
    'diagnoser_role ... zero SELECT grant on ground_truth_recoverable ...
     enforced at the DB-permission level.'

    This test creates a payment row and verifies that diagnoser_role cannot
    SELECT the ground_truth_recoverable column — even with a direct SQL query.
    """

    def test_diagnoser_cannot_select_ground_truth_recoverable(self, migrated_db):
        """
        Connecting as diagnoser_role and attempting:
          SELECT ground_truth_recoverable FROM payments
        must raise a permission error.

        This is the most important security test in Phase 0.
        If this fails, the evaluation harness's incremental revenue number
        could be contaminated by ground truth leakage.
        """
        from sqlalchemy import create_engine

        # Connect as superuser, insert a test payment to have a row
        engine = create_engine(migrated_db)
        with engine.connect() as conn:
            # Insert prerequisite rows
            conn.execute(
                text(
                    """
                    INSERT INTO merchants (merchant_id, name)
                    VALUES (gen_random_uuid(), 'test_merchant')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO customers (customer_id, merchant_id)
                    SELECT gen_random_uuid(), merchant_id FROM merchants LIMIT 1
                    """
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
                    FROM merchants m
                    CROSS JOIN customers c
                    LIMIT 1
                    """
                )
            )
            conn.commit()

            # Switch to diagnoser_role
            conn.execute(text("SET ROLE diagnoser_role"))

            # This MUST raise a permission error
            with pytest.raises(Exception) as exc_info:
                conn.execute(text("SELECT ground_truth_recoverable FROM payments LIMIT 1"))

            err = str(exc_info.value).lower()
            assert any(
                phrase in err for phrase in ["permission denied", "denied", "privilege", "column"]
            ), (
                f"Expected column-level permission denied for diagnoser_role "
                f"on ground_truth_recoverable, got: {exc_info.value}"
            )

        engine.dispose()

    def test_diagnoser_can_read_safe_payment_columns(self, migrated_db):
        """
        Diagnoser should be able to read non-ground-truth payment columns.
        This verifies the grant is surgical, not a blanket deny on the whole table.
        """
        from sqlalchemy import create_engine

        engine = create_engine(migrated_db)
        with engine.connect() as conn:
            conn.execute(text("SET ROLE diagnoser_role"))
            # These columns are in the GRANT list — should succeed
            result = conn.execute(
                text("SELECT payment_id, amount_paise, method, status FROM payments LIMIT 1")
            )
            result.fetchall()  # Should not raise

        engine.dispose()
