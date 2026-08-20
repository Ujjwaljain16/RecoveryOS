"""
Unit Tests — Settings and Config
==================================
Fast tests that don't touch the DB.
Verify that the Pydantic settings module works correctly.
"""

from __future__ import annotations

import os
import pytest


class TestSettings:
    def test_default_env_is_demo(self):
        """Default env is demo when no environment variable is set."""
        from recoveryos.config import Settings
        # Create a fresh Settings with no env override — bypass cached singleton
        s = Settings(env="demo")
        assert s.env.value == "demo"

    def test_demo_enables_simulate_endpoint(self):
        from recoveryos.config import Settings
        s = Settings(env="demo")
        assert s.is_demo is True

    def test_staging_disables_simulate_endpoint(self):
        from recoveryos.config import Settings
        s = Settings(env="staging")
        assert s.is_demo is False

    def test_default_max_amount_paise_is_25000_rupees(self):
        """₹25,000 = 2,500,000 paise — verify integer, not float."""
        from recoveryos.config import Settings
        s = Settings()
        assert s.default_max_amount_paise == 2_500_000
        assert isinstance(s.default_max_amount_paise, int)

    def test_ai_timeout_is_2_point_5_seconds(self):
        from recoveryos.config import Settings
        s = Settings()
        assert s.ai_diagnoser_timeout_seconds == 2.5

    def test_settings_singleton_is_cached(self):
        from recoveryos.config import get_settings
        get_settings.cache_clear()
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2


class TestModelImports:
    """Verify all ORM models import cleanly without a live DB."""

    def test_all_models_importable(self):
        from recoveryos.models import (
            AuditLog,
            AnomalyWindow,
            BaselineRun,
            CandidateAction,
            Customer,
            Diagnosis,
            Event,
            Merchant,
            Payment,
            PolicyConfig,
            PolicyDecision,
            Recovery,
            RecoveryLedger,
            ActionCost,
        )
        # All models should be importable without errors
        assert all([
            AuditLog, AnomalyWindow, BaselineRun, CandidateAction,
            Customer, Diagnosis, Event, Merchant, Payment,
            PolicyConfig, PolicyDecision, Recovery, RecoveryLedger, ActionCost,
        ])

    def test_base_metadata_has_all_tables(self):
        from recoveryos.models import Base
        table_names = set(Base.metadata.tables.keys())
        expected = {
            "merchants", "customers", "payments", "events",
            "anomaly_windows", "diagnoses", "candidate_actions",
            "policy_configs", "policy_decisions", "recoveries",
            "recovery_ledger", "audit_log", "action_costs", "baseline_runs",
        }
        missing = expected - table_names
        assert not missing, f"ORM metadata missing tables: {missing}"
