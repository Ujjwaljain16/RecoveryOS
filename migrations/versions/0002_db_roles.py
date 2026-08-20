"""DB roles and permission hardening — TRD §1.3, §9, gaps.md §B.1

Revision ID: 0002
Revises: 0001

What this migration does:
  1. Creates app_role — full R/W on all tables EXCEPT:
       - audit_log and events get NO UPDATE, NO DELETE (immutability enforcement)
  2. Creates diagnoser_role — SELECT on safe payment columns only:
       - Explicitly NO SELECT on payments.ground_truth_recoverable
  3. Creates the recoveryos and diagnoser DB users.

Security rationale (TRD §9):
  "REVOKE UPDATE, DELETE on audit_log, events FROM app_role at DB grant level,
   not just app-layer discipline — so even a bug can't silently rewrite history."
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision: str = "0002"
down_revision: str | None = "0001"
branch_labels = None
depends_on = None

ALL_TABLES = [
    "merchants", "customers", "payments", "events",
    "anomaly_windows", "diagnoses", "candidate_actions",
    "policy_configs", "policy_decisions", "recoveries",
    "recovery_ledger", "audit_log", "action_costs", "baseline_runs",
]

# app_role cannot UPDATE or DELETE these — immutability enforcement
APPEND_ONLY_TABLES = ["audit_log", "events"]

# diagnoser can SELECT from these tables (not payments — handled separately)
DIAGNOSER_SAFE_TABLES = [
    "merchants", "customers", "anomaly_windows", "diagnoses",
    "candidate_actions", "policy_configs", "policy_decisions",
    "recoveries", "recovery_ledger", "audit_log", "action_costs", "baseline_runs",
]

# Safe payment columns diagnoser_role CAN select — ground_truth_recoverable is NOT here
DIAGNOSER_PAYMENT_COLUMNS = (
    "payment_id, merchant_id, customer_id, amount_paise, method, bank, "
    "status, failure_code, failure_class, is_synthetic, created_at, failed_at"
)


def upgrade() -> None:
    conn = op.get_bind()

    # ── Create roles (idempotent via DO block) ─────────────────────────────
    conn.execute(sa.text("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'app_role') THEN
                CREATE ROLE app_role;
            END IF;
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'diagnoser_role') THEN
                CREATE ROLE diagnoser_role;
            END IF;
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'recoveryos') THEN
                CREATE USER recoveryos WITH PASSWORD 'recoveryos';
            END IF;
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'diagnoser') THEN
                CREATE USER diagnoser WITH PASSWORD 'diagnoser_pass';
            END IF;
        END
        $$;
    """))

    conn.execute(sa.text("GRANT app_role TO recoveryos;"))
    conn.execute(sa.text("GRANT diagnoser_role TO diagnoser;"))

    # ── app_role: full access to everything ───────────────────────────────
    for table in ALL_TABLES:
        conn.execute(sa.text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO app_role;"
        ))

    # ── app_role: REVOKE mutation on append-only tables ────────────────────
    # This is the TRD §9 immutability guarantee — DB-level, not app-layer.
    for table in APPEND_ONLY_TABLES:
        conn.execute(sa.text(f"REVOKE UPDATE, DELETE ON {table} FROM app_role;"))

    # ── diagnoser_role: SELECT on non-ground-truth tables ─────────────────
    for table in DIAGNOSER_SAFE_TABLES:
        conn.execute(sa.text(f"GRANT SELECT ON {table} TO diagnoser_role;"))

    # ── diagnoser_role: Column-level grant on payments ─────────────────────
    # Grant only the safe columns — ground_truth_recoverable is NOT included.
    # This is enforced at the Postgres column-privilege level, not app code.
    conn.execute(sa.text(
        f"GRANT SELECT ({DIAGNOSER_PAYMENT_COLUMNS}) ON payments TO diagnoser_role;"
    ))


def downgrade() -> None:
    conn = op.get_bind()

    for table in ALL_TABLES:
        conn.execute(sa.text(f"REVOKE ALL ON {table} FROM app_role;"))
    for table in DIAGNOSER_SAFE_TABLES:
        conn.execute(sa.text(f"REVOKE ALL ON {table} FROM diagnoser_role;"))
    conn.execute(sa.text("REVOKE ALL ON payments FROM diagnoser_role;"))

    conn.execute(sa.text("DROP USER IF EXISTS diagnoser;"))
    conn.execute(sa.text("DROP USER IF EXISTS recoveryos;"))
    conn.execute(sa.text("DROP ROLE IF EXISTS diagnoser_role;"))
    conn.execute(sa.text("DROP ROLE IF EXISTS app_role;"))
