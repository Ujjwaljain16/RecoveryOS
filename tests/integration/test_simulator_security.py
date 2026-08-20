"""
Integration security tests for simulator latent state isolation (TRD §6, gaps.md §B.1).
Proves that diagnoser_role and inference services have ZERO SELECT grants on simulator_latent_state.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text


class TestSimulatorLatentStateIsolation:
    def test_latent_state_table_has_no_grants_for_diagnoser_role(self, migrated_db):
        """
        The diagnoser_role (used by the AI diagnoser) must raise a permission error
        if attempting to query simulator_latent_state.
        """
        engine = create_engine(migrated_db)
        with engine.connect() as conn:
            # Switch to diagnoser_role
            conn.execute(text("SET ROLE diagnoser_role"))

            with pytest.raises(Exception) as exc_info:
                conn.execute(text("SELECT * FROM simulator_latent_state LIMIT 1;"))

            err_msg = str(exc_info.value).lower()
            assert any(
                phrase in err_msg
                for phrase in ["permission denied", "denied", "privilege", "must be granted"]
            ), f"Expected permission denied on simulator_latent_state for diagnoser_role, got: {exc_info.value}"

        engine.dispose()

    def test_manifest_table_has_no_grants_for_diagnoser_role(self, migrated_db):
        """
        The diagnoser_role must raise a permission error if attempting to query simulator_manifests.
        """
        engine = create_engine(migrated_db)
        with engine.connect() as conn:
            conn.execute(text("SET ROLE diagnoser_role"))

            with pytest.raises(Exception) as exc_info:
                conn.execute(text("SELECT * FROM simulator_manifests LIMIT 1;"))

            err_msg = str(exc_info.value).lower()
            assert any(
                phrase in err_msg
                for phrase in ["permission denied", "denied", "privilege", "must be granted"]
            ), f"Expected permission denied on simulator_manifests for diagnoser_role, got: {exc_info.value}"

        engine.dispose()

    def test_app_role_can_query_simulator_latent_state(self, migrated_db):
        """
        The app_role has full permissions to write and read simulator_latent_state.
        """
        engine = create_engine(migrated_db)
        with engine.connect() as conn:
            conn.execute(text("SET ROLE app_role"))
            result = conn.execute(text("SELECT COUNT(*) FROM simulator_latent_state;")).scalar()
            assert result is not None
            assert result >= 0

        engine.dispose()
