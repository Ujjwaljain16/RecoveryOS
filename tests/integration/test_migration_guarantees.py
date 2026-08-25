"""
Direct tests of migrations/ guarantees — constraints and grants exercised
against real Postgres, not indirectly via other tests' use of the
migrated_db fixture as a precondition.

Task M4 (pre-Phase-8 audit): the migrations/ directory's own guarantees had
never been tested directly before this file. Houses:
  - test_duplicate_action_cost_row_rejected_at_insert (Task M1)
  - test_diagnoser_role_cannot_select_merchant_api_key_hash /
    test_inference_role_cannot_select_merchant_api_key_hash (Task M2)
  - test_audit_log_and_events_are_genuinely_append_only_at_role_level
    (consolidated here so "does this migration's guarantee have a direct
    test" is answerable in one file, not scattered across
    test_schema_and_roles.py)
  - test_recovery_episodes_table_is_gone (Task M3)

Connects as the real login role (diagnoser/inference DSNs from
conftest.py's diagnoser_database_url()/inference_database_url()) rather
than SET ROLE, per the Phase 4 methodology — SET ROLE as the test
superuser can behave differently from a real login connection in edge
cases, so the strongest proof uses the actual credentialed user.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import diagnoser_database_url, inference_database_url


# ─── Task M1: action_costs UNIQUE constraint ───────────────────────────────


class TestActionCostsUniqueConstraint:
    def test_duplicate_action_cost_row_rejected_at_insert(self, migrated_db):
        """
        Two inserts for the identical (merchant_id=NULL, action_type,
        version) must collide at INSERT time (IntegrityError), not silently
        create a duplicate that later breaks evi.py's .scalar_one_or_none()
        at read time.

        Uses a test-only action_type string (action_type has no CHECK
        constraint — plain Text, migrations/0001) rather than a real one
        like 'RETRY_NOW': migrated_db is shared across the whole test
        session, and get_action_cost() (services/recovery_engine/evi.py)
        looks up the platform-default row by action_type alone, with no
        version filter — inserting a second real-action_type row here would
        leak into every other test's action-cost resolution and manifest as
        a spurious MultipleResultsFound somewhere else entirely (the exact
        test-isolation trap this audit's ground rules exist to catch).
        """
        engine = create_engine(migrated_db)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO action_costs (merchant_id, action_type, version, "
                    "cost_paise, friction_base_paise) "
                    "VALUES (NULL, 'TEST_ONLY_ACTION_TYPE_M1', 999, 0, 10)"
                )
            )
            conn.commit()

            with pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        "INSERT INTO action_costs (merchant_id, action_type, version, "
                        "cost_paise, friction_base_paise) "
                        "VALUES (NULL, 'TEST_ONLY_ACTION_TYPE_M1', 999, 0, 10)"
                    )
                )
                conn.commit()
        engine.dispose()

    def test_duplicate_merchant_scoped_action_cost_row_rejected_at_insert(self, migrated_db):
        """Same collision, but for a real (non-NULL) merchant_id — the
        COALESCE trick must not accidentally only cover the NULL case."""
        engine = create_engine(migrated_db)
        merchant_id = str(uuid.uuid4())
        with engine.connect() as conn:
            conn.execute(
                text("INSERT INTO merchants (merchant_id, name) VALUES (:mid, 'test_merchant')"),
                {"mid": merchant_id},
            )
            conn.execute(
                text(
                    "INSERT INTO action_costs (merchant_id, action_type, version, "
                    "cost_paise, friction_base_paise) "
                    "VALUES (:mid, 'ESCALATE', 1000, 0, 0)"
                ),
                {"mid": merchant_id},
            )
            conn.commit()

            with pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        "INSERT INTO action_costs (merchant_id, action_type, version, "
                        "cost_paise, friction_base_paise) "
                        "VALUES (:mid, 'ESCALATE', 1000, 0, 0)"
                    ),
                    {"mid": merchant_id},
                )
                conn.commit()
        engine.dispose()

    def test_two_different_merchants_can_share_the_same_action_type_and_version(self, migrated_db):
        """Sanity: the constraint must be scoped PER merchant, not global —
        two distinct merchants each having their own version-1000 ESCALATE
        row is legitimate and must not collide."""
        engine = create_engine(migrated_db)
        merchant_a, merchant_b = str(uuid.uuid4()), str(uuid.uuid4())
        with engine.connect() as conn:
            for mid in (merchant_a, merchant_b):
                conn.execute(
                    text("INSERT INTO merchants (merchant_id, name) VALUES (:mid, 'test_merchant')"),
                    {"mid": mid},
                )
                conn.execute(
                    text(
                        "INSERT INTO action_costs (merchant_id, action_type, version, "
                        "cost_paise, friction_base_paise) "
                        "VALUES (:mid, 'ALT_ROUTE', 2000, 0, 0)"
                    ),
                    {"mid": mid},
                )
            conn.commit()  # must not raise
        engine.dispose()


# ─── Task M2: merchants.api_key_hash column-level grant ───────────────────


class TestMerchantApiKeyHashColumnGrants:
    def test_diagnoser_role_cannot_select_merchant_api_key_hash(self, migrated_db):
        url = diagnoser_database_url(migrated_db).replace("+asyncpg", "+psycopg2")
        engine = create_engine(url)
        with pytest.raises(Exception) as exc_info:
            with engine.connect() as conn:
                conn.execute(text("SELECT api_key_hash FROM merchants LIMIT 1"))
        err = str(exc_info.value).lower()
        assert any(p in err for p in ["permission denied", "denied", "privilege", "column"]), (
            f"diagnoser_role must not be able to SELECT merchants.api_key_hash, got: {exc_info.value}"
        )
        engine.dispose()

    def test_inference_role_cannot_select_merchant_api_key_hash(self, migrated_db):
        url = inference_database_url(migrated_db).replace("+asyncpg", "+psycopg2")
        engine = create_engine(url)
        with pytest.raises(Exception) as exc_info:
            with engine.connect() as conn:
                conn.execute(text("SELECT api_key_hash FROM merchants LIMIT 1"))
        err = str(exc_info.value).lower()
        assert any(p in err for p in ["permission denied", "denied", "privilege", "column"]), (
            f"inference_role must not be able to SELECT merchants.api_key_hash, got: {exc_info.value}"
        )
        engine.dispose()

    def test_diagnoser_role_can_still_read_safe_merchant_columns(self, migrated_db):
        """The fix must be surgical — diagnoser_role's legitimate reads
        (merchant_id, name, policy_config_id, created_at) must keep working."""
        url = diagnoser_database_url(migrated_db).replace("+asyncpg", "+psycopg2")
        engine = create_engine(url)
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT merchant_id, name, policy_config_id, created_at FROM merchants LIMIT 1")
            )
            result.fetchall()  # must not raise
        engine.dispose()

    def test_inference_role_can_still_read_safe_merchant_columns(self, migrated_db):
        url = inference_database_url(migrated_db).replace("+asyncpg", "+psycopg2")
        engine = create_engine(url)
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT merchant_id, name, policy_config_id, created_at FROM merchants LIMIT 1")
            )
            result.fetchall()  # must not raise
        engine.dispose()


# ─── Task M3: recovery_episodes dropped ────────────────────────────────────


def test_recovery_episodes_table_is_gone(migrated_db):
    """
    0004_episodes.py's recovery_episodes was schema for a design (DB-backed
    episode persistence) superseded by the Parquet-based
    simulator/dataset/builder.py before anything ever wrote to it —
    0012_drop_recovery_episodes.py removes it. Confirm a fresh
    `alembic upgrade head` genuinely doesn't have the table anymore.
    """
    engine = create_engine(migrated_db)
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'recovery_episodes'"
            )
        )
        assert result.fetchall() == [], "recovery_episodes must not exist after migration 0012"
    engine.dispose()


# ─── Consolidated: audit_log / events append-only, at real-role level ─────


class TestAppendOnlyTablesRejectMutation:
    """
    TRD §9's immutability guarantee, re-run here as the standing
    "does this migration's guarantee have a direct test" home for
    append-only enforcement — test_schema_and_roles.py's equivalents use
    SET ROLE app_role from a superuser connection; kept there untouched,
    this is the same guarantee for completeness of this file's index.
    """

    def test_audit_log_and_events_are_genuinely_append_only_at_role_level(self, migrated_db):
        engine = create_engine(migrated_db)
        with engine.connect() as conn:
            conn.execute(
                text("INSERT INTO audit_log (audit_id, summary) VALUES (gen_random_uuid(), 'm4-append-only')")
            )
            conn.commit()

            conn.execute(text("SET ROLE app_role"))

            with pytest.raises(Exception) as exc_info:
                conn.execute(text("UPDATE audit_log SET summary = 'tampered'"))
                conn.commit()
            assert "permission denied" in str(exc_info.value).lower()

            conn.rollback()
            conn.execute(text("SET ROLE app_role"))
            with pytest.raises(Exception) as exc_info:
                conn.execute(text("DELETE FROM audit_log"))
                conn.commit()
            assert "permission denied" in str(exc_info.value).lower()

            conn.rollback()
            conn.execute(text("SET ROLE app_role"))
            with pytest.raises(Exception) as exc_info:
                conn.execute(text("UPDATE events SET event_type = 'TAMPERED'"))
                conn.commit()
            assert "permission denied" in str(exc_info.value).lower()

        engine.dispose()
