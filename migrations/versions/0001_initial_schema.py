"""Initial schema — every table from TRD §2 verbatim.

Revision ID: 0001
Revises:
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    # ─── policy_configs ───────────────────────────────────────────────────────
    # Must come before merchants (FK dependency)
    op.create_table(
        "policy_configs",
        sa.Column(
            "policy_config_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("retry_cooldown_hours", sa.Integer(), nullable=False, server_default="12"),
        # ₹25,000 in paise — BIGINT, never Float
        sa.Column("max_amount_paise", sa.BigInteger(), nullable=False, server_default="2500000"),
        sa.Column("stop_after_success", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("stop_after_opt_out", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("escalate_after_failures", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("min_expected_value_paise", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ─── merchants ────────────────────────────────────────────────────────────
    op.create_table(
        "merchants",
        sa.Column(
            "merchant_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "policy_config_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("policy_configs.policy_config_id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ─── customers ────────────────────────────────────────────────────────────
    op.create_table(
        "customers",
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "merchant_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("merchants.merchant_id"),
            nullable=False,
        ),
        sa.Column("is_returning", sa.Boolean(), nullable=False, server_default="false"),
        # BIGINT paise — never Float
        sa.Column("lifetime_value_paise", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("opted_out_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ─── payments ─────────────────────────────────────────────────────────────
    op.create_table(
        "payments",
        sa.Column(
            "payment_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "merchant_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("merchants.merchant_id"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("customers.customer_id"),
            nullable=False,
        ),
        # BIGINT paise — critical: never Float
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("bank", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column("failure_class", sa.Text(), nullable=True),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False, server_default="true"),
        # ─── GROUND TRUTH — SIMULATOR ONLY ───────────────────────────────────
        # diagnoser_role has NO SELECT grant on this column.
        # inference_role has NO SELECT grant on this column.
        # SELECT * is BANNED in all inference-reachable code (CI enforced).
        sa.Column("ground_truth_recoverable", sa.Boolean(), nullable=True),
        # ─────────────────────────────────────────────────────────────────────
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("failed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("amount_paise > 0", name="ck_payments_amount_positive"),
    )
    op.create_index("idx_payments_merchant_status", "payments", ["merchant_id", "status"])
    op.create_index("idx_payments_bank_method_time", "payments", ["bank", "method", "failed_at"])

    # ─── events (append-only) ─────────────────────────────────────────────────
    op.create_table(
        "events",
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "payment_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("payments.payment_id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "occurred_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("idx_events_payment", "events", ["payment_id", "occurred_at"])

    # ─── anomaly_windows ──────────────────────────────────────────────────────
    op.create_table(
        "anomaly_windows",
        sa.Column(
            "window_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_entity", sa.Text(), nullable=False),
        sa.Column("time_bucket", postgresql.TIMESTAMP(timezone=True), nullable=False),
        # These are rates (0.0-1.0), not money — NUMERIC is correct here
        sa.Column("baseline_rate", sa.Numeric(5, 4), nullable=True),
        sa.Column("observed_rate", sa.Numeric(5, 4), nullable=True),
        sa.Column("z_score", sa.Numeric(6, 3), nullable=True),
        sa.Column("severity", sa.Text(), nullable=True),
        sa.Column("is_anomaly", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "scope_type", "scope_entity", "time_bucket", name="uq_anomaly_scope_bucket"
        ),
    )

    # ─── diagnoses ────────────────────────────────────────────────────────────
    op.create_table(
        "diagnoses",
        sa.Column(
            "diagnosis_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "payment_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("payments.payment_id"),
            nullable=True,
        ),
        sa.Column("cohort_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("root_cause", sa.Text(), nullable=False),
        # Confidence 0.0-1.0 — NOT money, NUMERIC ok. Capped at 0.6 for fallback.
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        # Added per gaps.md §A.3 — makes fallback path transparent in Audit Explorer
        sa.Column("is_fallback", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ─── candidate_actions ────────────────────────────────────────────────────
    op.create_table(
        "candidate_actions",
        sa.Column(
            "candidate_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "payment_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("payments.payment_id"),
            nullable=False,
        ),
        sa.Column("action_type", sa.Text(), nullable=False),
        # INTEGER basis points (0-10000), NOT float. 82.00% = 8200.
        # gaps.md §B.4: all EVI arithmetic is integer-only.
        sa.Column("recovery_prob_bps", sa.Integer(), nullable=False),
        # BIGINT paise — computed as (amount_paise * recovery_prob_bps) // 10_000
        sa.Column("expected_value_paise", sa.BigInteger(), nullable=False),
        sa.Column("cost_paise", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("friction_penalty_paise", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("risk_penalty_paise", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ─── policy_decisions ─────────────────────────────────────────────────────
    op.create_table(
        "policy_decisions",
        sa.Column(
            "decision_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "payment_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("payments.payment_id"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("candidate_actions.candidate_id"),
            nullable=False,
        ),
        sa.Column(
            "policy_config_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("policy_configs.policy_config_id"),
            nullable=False,
        ),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("rule_trace", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ─── recoveries ───────────────────────────────────────────────────────────
    op.create_table(
        "recoveries",
        sa.Column(
            "recovery_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "payment_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("payments.payment_id"),
            nullable=False,
        ),
        sa.Column(
            "decision_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("policy_decisions.decision_id"),
            nullable=False,
        ),
        # UNIQUE at DB level — physical backstop against double-execution (gaps.md §B.2)
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("scheduled_for", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("executed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        # BIGINT paise — never Float
        sa.Column("recovered_amount_paise", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("provider_ref", sa.Text(), nullable=True),
        sa.Column("stopping_rule_triggered", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("idx_recoveries_payment", "recoveries", ["payment_id"])

    # ─── recovery_ledger ──────────────────────────────────────────────────────
    op.create_table(
        "recovery_ledger",
        sa.Column(
            "ledger_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "payment_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("payments.payment_id"),
            nullable=False,
        ),
        # All BIGINT paise — evaluation harness SUM()s these directly
        sa.Column("revenue_at_risk_paise", sa.BigInteger(), nullable=False),
        sa.Column("expected_recovery_paise", sa.BigInteger(), nullable=False),
        sa.Column("actual_recovery_paise", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("baseline_outcome", sa.Text(), nullable=True),
        sa.Column(
            "incremental_recovery_paise",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "intervention_cost_paise",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("net_recovery_paise", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ─── audit_log (INSERT-ONLY — UPDATE/DELETE revoked in next migration) ────
    op.create_table(
        "audit_log",
        sa.Column(
            "audit_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "payment_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("payments.payment_id"),
            nullable=True,
        ),
        sa.Column(
            "diagnosis_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("diagnoses.diagnosis_id"),
            nullable=True,
        ),
        sa.Column(
            "candidate_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("candidate_actions.candidate_id"),
            nullable=True,
        ),
        sa.Column(
            "decision_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("policy_decisions.decision_id"),
            nullable=True,
        ),
        sa.Column(
            "recovery_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("recoveries.recovery_id"),
            nullable=True,
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ─── action_costs (gaps.md §A.2) ─────────────────────────────────────────
    op.create_table(
        "action_costs",
        sa.Column(
            "action_cost_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "merchant_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("merchants.merchant_id"),
            nullable=True,
        ),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("cost_paise", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("friction_base_paise", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "effective_from",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "idx_action_cost_merchant_action",
        "action_costs",
        ["merchant_id", "action_type", "version"],
    )

    # ─── baseline_runs (evaluation harness) ───────────────────────────────────
    op.create_table(
        "baseline_runs",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("experiment_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "payment_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("payments.payment_id"),
            nullable=False,
        ),
        sa.Column("recovered_amount_paise", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ─── Seed platform-default action costs (gaps.md §A.2) ────────────────────
    op.execute(
        """
        INSERT INTO action_costs (action_type, cost_paise, friction_base_paise)
        VALUES
            ('RETRY_NOW',    0,     10),
            ('RETRY_LATER',  0,     0),
            ('ALT_ROUTE',    200,   50),
            ('REMINDER',     20,    30),
            ('ESCALATE',     15000, 100),
            ('DO_NOTHING',   0,     0)
        """
    )


def downgrade() -> None:
    op.drop_table("baseline_runs")
    op.drop_table("action_costs")
    op.drop_table("audit_log")
    op.drop_table("recovery_ledger")
    op.drop_index("idx_recoveries_payment", table_name="recoveries")
    op.drop_table("recoveries")
    op.drop_table("policy_decisions")
    op.drop_table("candidate_actions")
    op.drop_table("diagnoses")
    op.drop_table("anomaly_windows")
    op.drop_index("idx_events_payment", table_name="events")
    op.drop_table("events")
    op.drop_index("idx_payments_bank_method_time", table_name="payments")
    op.drop_index("idx_payments_merchant_status", table_name="payments")
    op.drop_table("payments")
    op.drop_table("customers")
    op.drop_table("merchants")
    op.drop_table("policy_configs")
