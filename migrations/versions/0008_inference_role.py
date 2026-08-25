"""inference_role — gaps.md §B.1, TRD §3.3

Revision ID: 0008
Revises: 0007

Mirrors diagnoser_role exactly (same rationale: the propensity model's
feature-building step must never be able to SELECT ground_truth_recoverable,
even by accident via a SELECT * — enforced at the Postgres GRANT level, not
just the Python-side ALLOWED_FEATURE_COLUMNS allow-list in
services/recovery_engine/propensity.py). A separate role from diagnoser_role
(rather than reusing it) keeps the two inference paths independently
auditable/revocable even though their grants happen to be identical today.
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels = None
depends_on = None

# Same safe-table list as diagnoser_role (migrations/0002) — the propensity
# model needs merchants/customers (is_returning, lifetime_value_paise) and
# anomaly_windows (bank_current_success_rate context) at minimum; granting
# the same read surface as the diagnoser keeps the two restricted roles
# trivially comparable in a permissions audit.
INFERENCE_SAFE_TABLES = [
    "merchants",
    "customers",
    "anomaly_windows",
    "diagnoses",
    "candidate_actions",
    "policy_configs",
    "policy_decisions",
    "recoveries",
    "recovery_ledger",
    "audit_log",
    "action_costs",
    "baseline_runs",
]

# Ground_truth_recoverable is NOT here — identical column allow-list to
# diagnoser_role's DIAGNOSER_PAYMENT_COLUMNS (migrations/0002).
INFERENCE_PAYMENT_COLUMNS = (
    "payment_id, merchant_id, customer_id, amount_paise, method, bank, "
    "status, failure_code, failure_class, is_synthetic, created_at, failed_at"
)


def _required_env_password(var_name: str) -> str:
    value = os.environ.get(var_name)
    if not value:
        raise RuntimeError(
            f"{var_name} is not set. This migration requires it to create the "
            f"DB role's password — see .env.example. Refusing to fall back to "
            f"any default."
        )
    return value.replace("'", "''")


def upgrade() -> None:
    conn = op.get_bind()

    inference_role_password = _required_env_password("RECOVERYOS_INFERENCE_ROLE_PASSWORD")

    conn.execute(
        sa.text(
            f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'inference_role') THEN
                CREATE ROLE inference_role;
            END IF;
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'inference') THEN
                CREATE USER inference WITH PASSWORD '{inference_role_password}';
            ELSE
                ALTER USER inference WITH PASSWORD '{inference_role_password}';
            END IF;
        END
        $$;
    """
        )
    )

    conn.execute(sa.text("GRANT inference_role TO inference;"))

    for table in INFERENCE_SAFE_TABLES:
        conn.execute(sa.text(f"GRANT SELECT ON {table} TO inference_role;"))

    conn.execute(
        sa.text(f"GRANT SELECT ({INFERENCE_PAYMENT_COLUMNS}) ON payments TO inference_role;")
    )

    # Explicit belt-and-suspenders: zero grants on simulator-only tables,
    # matching diagnoser_role's isolation from migrations/0003.
    conn.execute(sa.text("REVOKE ALL ON simulator_manifests FROM inference_role;"))
    conn.execute(sa.text("REVOKE ALL ON simulator_latent_state FROM inference_role;"))


def downgrade() -> None:
    conn = op.get_bind()

    for table in INFERENCE_SAFE_TABLES:
        conn.execute(sa.text(f"REVOKE ALL ON {table} FROM inference_role;"))
    conn.execute(sa.text("REVOKE ALL ON payments FROM inference_role;"))

    conn.execute(sa.text("DROP USER IF EXISTS inference;"))
    conn.execute(sa.text("DROP ROLE IF EXISTS inference_role;"))
