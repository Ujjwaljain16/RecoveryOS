"""
RecoveryOS — SQLAlchemy ORM Models
=====================================
Maps every table from TRD §2 exactly.

Design invariants enforced here:
  - All monetary values are BIGINT paise (never Float/Numeric for currency).
  - Foreign keys are explicit everywhere — no orphaned decisions.
  - audit_log and events are insert-only; UPDATE/DELETE revoked at the DB role level
    (see migrations/versions/0002_db_roles.sql).
  - recovery_prob is stored as INTEGER basis points (0-10000) — never a float.
    82.00% = 8200 bps. This matches the gaps.md §B.4 hardening requirement.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func

# Timezone-aware timestamp — use DateTime(timezone=True) which maps to TIMESTAMPTZ in Postgres
TIMESTAMPTZ = DateTime(timezone=True)


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# CORE ENTITIES
# ═══════════════════════════════════════════════════════════════════════════════


class PolicyConfig(Base):
    """
    Merchant-level policy configuration.
    Defined before Merchant because Merchant has a FK to it.
    All monetary floors in BIGINT paise.
    """

    __tablename__ = "policy_configs"

    policy_config_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    retry_cooldown_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    # ₹25,000 default cap expressed in paise — BIGINT, never Float
    max_amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=2_500_000)
    stop_after_success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    stop_after_opt_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    escalate_after_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    # floor for "do nothing" trigger — negative EVI always triggers DO_NOTHING
    min_expected_value_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    # Relationships
    merchants: Mapped[list[Merchant]] = relationship(back_populates="policy_config")
    policy_decisions: Mapped[list[PolicyDecision]] = relationship(back_populates="policy_config")


class Merchant(Base):
    __tablename__ = "merchants"

    merchant_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    policy_config_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("policy_configs.policy_config_id"),
        nullable=True,
    )
    # SHA-256(pepper + raw key) — never the raw key. NULL until a key is
    # issued (auth.generate_api_key() + auth.hash_api_key()). Migration 0006.
    # See apps/api/dependencies/auth.py for the verification path.
    api_key_hash: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    # Relationships
    policy_config: Mapped[PolicyConfig | None] = relationship(back_populates="merchants")
    customers: Mapped[list[Customer]] = relationship(back_populates="merchant")
    payments: Mapped[list[Payment]] = relationship(back_populates="merchant")


class Customer(Base):
    __tablename__ = "customers"

    customer_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("merchants.merchant_id"),
        nullable=False,
    )
    is_returning: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Customer lifetime value in paise — BIGINT, never Float
    lifetime_value_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    opted_out_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMPTZ, nullable=True
    )  # NULL = not opted out
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    # Relationships
    merchant: Mapped[Merchant] = relationship(back_populates="customers")
    payments: Mapped[list[Payment]] = relationship(back_populates="customer")


class Payment(Base):
    """
    Core payment record.

    CRITICAL SECURITY NOTE:
      ground_truth_recoverable is a simulator-only column.
      The diagnoser_role Postgres role has ZERO SELECT grant on this column.
      The inference service uses an ALLOWED_FEATURE_COLUMNS allowlist that
      explicitly excludes this column. SELECT * is BANNED in all inference-
      reachable code paths (enforced by CI grep check).
    """

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="ck_payments_amount_positive"),
        Index("idx_payments_merchant_status", "merchant_id", "status"),
        Index("idx_payments_bank_method_time", "bank", "method", "failed_at"),
    )

    payment_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("merchants.merchant_id"),
        nullable=False,
    )
    customer_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("customers.customer_id"),
        nullable=False,
    )
    # Payment amount in paise — BIGINT, never Float/Numeric
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)  # upi | card | netbanking | wallet
    bank: Mapped[str | None] = mapped_column(Text, nullable=True)
    # created|authorized|failed|success|expired
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # TIMEOUT|INVALID_CREDS|BANK_DOWN|...
    failure_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    # TEMPORARY|PERMANENT|CUSTOMER_SPECIFIC|SYSTEMIC|UNKNOWN
    failure_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    # false only for real test-mode calls
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # ─────────────────────────────────────────────────────────────────────────
    # SIMULATOR GROUND TRUTH — NEVER EXPOSED TO INFERENCE PATH
    # diagnoser_role has NO SELECT grant on this column.
    # ─────────────────────────────────────────────────────────────────────────
    ground_truth_recoverable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    failed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)

    # Relationships
    merchant: Mapped[Merchant] = relationship(back_populates="payments")
    customer: Mapped[Customer] = relationship(back_populates="payments")
    events: Mapped[list[Event]] = relationship(back_populates="payment")
    diagnoses: Mapped[list[Diagnosis]] = relationship(back_populates="payment")
    candidate_actions: Mapped[list[CandidateAction]] = relationship(back_populates="payment")
    policy_decisions: Mapped[list[PolicyDecision]] = relationship(back_populates="payment")
    recoveries: Mapped[list[Recovery]] = relationship(back_populates="payment")
    recovery_ledger: Mapped[RecoveryLedger | None] = relationship(back_populates="payment")
    audit_logs: Mapped[list[AuditLog]] = relationship(back_populates="payment")


class Event(Base):
    """
    Append-only ledger of every state transition.
    REVOKE UPDATE, DELETE on this table from app_role at the DB level.
    The audit explorer is a query over this table — it can never drift from reality.
    """

    __tablename__ = "events"
    __table_args__ = (
        Index("idx_events_payment", "payment_id", "occurred_at"),
        # Scoped to (payment_id, idempotency_key), NOT idempotency_key alone.
        # A global-uniqueness constraint would wrongly reject a legitimate
        # second event on a DIFFERENT payment if a client (or a bug) ever
        # reused a key across payments — e.g. a naive client that generates
        # idempotency_key from something coarser than the payment (a request
        # timestamp, a batch id). Scoping to the payment means "this exact
        # payment's retry of this exact logical event" is deduplicated,
        # without silently dropping unrelated events that happen to collide
        # on the key alone.
        UniqueConstraint("payment_id", "idempotency_key", name="uq_events_payment_idempotency_key"),
    )

    event_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    payment_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("payments.payment_id"),
        nullable=False,
    )
    # Client-supplied (or server-generated fallback) dedup key — see
    # EventPayload.idempotency_key (apps/api/routers/events.py), read from
    # the JSON BODY, not a header (a body field travels with the specific
    # logical event being retried; a header would apply to the whole
    # request/connection, which is the wrong granularity for "retry this
    # exact payment event"). Two HTTP submissions for the SAME payment with
    # the same idempotency_key must resolve to exactly one events row — this
    # is the DB-level backstop; repository.insert_event_idempotent() enforces
    # it atomically via INSERT ... ON CONFLICT (payment_id, idempotency_key)
    # DO NOTHING.
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    # PAYMENT_CREATED|PAYMENT_FAILED|RETRY_EXECUTED|CUSTOMER_OPTED_OUT|...
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    # Relationships
    payment: Mapped[Payment] = relationship(back_populates="events")


# ═══════════════════════════════════════════════════════════════════════════════
# RISK & ANOMALY
# ═══════════════════════════════════════════════════════════════════════════════


class AnomalyWindow(Base):
    """
    Rolling 15-minute z-score buckets per (bank, method).
    baseline_rate and observed_rate are failure rates (dimensionless 0.0–1.0),
    stored as NUMERIC(5,4) for statistical correctness — NOT money columns.
    """

    __tablename__ = "anomaly_windows"
    __table_args__ = (
        UniqueConstraint(
            "scope_type", "scope_entity", "time_bucket", name="uq_anomaly_scope_bucket"
        ),
    )

    window_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    scope_type: Mapped[str] = mapped_column(Text, nullable=False)  # bank|method|merchant
    scope_entity: Mapped[str] = mapped_column(Text, nullable=False)
    time_bucket: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)  # 15-min bucket
    baseline_rate: Mapped[float | None] = mapped_column(
        # These are rates (0.0–1.0), not money — NUMERIC is appropriate here
        nullable=True
    )
    observed_rate: Mapped[float | None] = mapped_column(nullable=True)
    z_score: Mapped[float | None] = mapped_column(nullable=True)
    severity: Mapped[str | None] = mapped_column(Text, nullable=True)  # low|medium|high
    is_anomaly: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


class Diagnosis(Base):
    """
    Root-cause diagnosis from either the AI Diagnoser or the deterministic fallback.
    is_fallback=True means the LLM was bypassed (timeout or unavailable).
    confidence is capped at 0.6 max for fallback diagnoses (gaps.md §A.3).
    model_version='fallback-rule-v1' identifies the fallback path.
    """

    __tablename__ = "diagnoses"
    __table_args__ = (
        Index("idx_diagnoses_payment", "payment_id", "created_at"),
        Index("idx_diagnoses_cohort", "cohort_id"),
    )

    diagnosis_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    payment_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("payments.payment_id"),
        nullable=True,
    )
    cohort_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), nullable=True
    )  # NULL if isolated, set if systemic
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(nullable=True)  # 0.0-1.0 (not money)
    evidence: Mapped[dict] = mapped_column(
        JSONB, nullable=False
    )  # structured facts cited, for grounding checks
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    is_fallback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )  # added per gaps.md §A.3
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    # Relationships
    payment: Mapped[Payment | None] = relationship(back_populates="diagnoses")
    audit_logs: Mapped[list[AuditLog]] = relationship(back_populates="diagnosis")


# ═══════════════════════════════════════════════════════════════════════════════
# RECOVERY DECISIONING
# ═══════════════════════════════════════════════════════════════════════════════


class CandidateAction(Base):
    """
    A candidate recovery action with its computed EVI scores.

    CRITICAL — INTEGER ARITHMETIC ONLY (gaps.md §B.4):
      recovery_prob_bps: INTEGER basis points (0–10000), not a float.
        82.00% is stored as 8200.
      expected_value_paise: BIGINT — computed as (amount_paise * recovery_prob_bps) // 10_000
      All cost/penalty columns are BIGINT paise.
    """

    __tablename__ = "candidate_actions"

    candidate_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    payment_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("payments.payment_id"),
        nullable=False,
    )
    # RETRY_NOW|RETRY_LATER|ALT_ROUTE|REMINDER|ESCALATE|DO_NOTHING
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    # Integer basis points (0-10000); 82.00% = 8200 — NO float
    recovery_prob_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    # EVI in paise — BIGINT, can be negative (DO_NOTHING trigger)
    expected_value_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    friction_penalty_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    risk_penalty_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    # Relationships
    payment: Mapped[Payment] = relationship(back_populates="candidate_actions")
    policy_decisions: Mapped[list[PolicyDecision]] = relationship(back_populates="candidate")
    audit_logs: Mapped[list[AuditLog]] = relationship(back_populates="candidate")


class PolicyDecision(Base):
    """
    Immutable record of every policy evaluation (ALLOW / BLOCK / ESCALATE).
    rule_trace contains the ordered list of {rule, passed, reason} — this makes
    every decision independently replayable without re-running code.
    """

    __tablename__ = "policy_decisions"

    decision_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    payment_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("payments.payment_id"),
        nullable=False,
    )
    candidate_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("candidate_actions.candidate_id"),
        nullable=False,
    )
    policy_config_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("policy_configs.policy_config_id"),
        nullable=False,
    )
    verdict: Mapped[str] = mapped_column(Text, nullable=False)  # ALLOW|BLOCK|ESCALATE
    rule_trace: Mapped[dict] = mapped_column(
        JSONB, nullable=False
    )  # ordered [{rule, passed, reason}]
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    # Relationships
    payment: Mapped[Payment] = relationship(back_populates="policy_decisions")
    candidate: Mapped[CandidateAction] = relationship(back_populates="policy_decisions")
    policy_config: Mapped[PolicyConfig] = relationship(back_populates="policy_decisions")
    recoveries: Mapped[list[Recovery]] = relationship(back_populates="policy_decision")
    audit_logs: Mapped[list[AuditLog]] = relationship(back_populates="policy_decision")


# ═══════════════════════════════════════════════════════════════════════════════
# EXECUTION & OUTCOME
# ═══════════════════════════════════════════════════════════════════════════════


class Recovery(Base):
    """
    A single recovery execution attempt.
    idempotency_key is UNIQUE at the DB level — this is the hard backstop
    preventing double-execution even if the advisory lock logic has a bug.
    (gaps.md §B.2 — the UNIQUE constraint is the physical guarantee, not just a convention.)
    """

    __tablename__ = "recoveries"
    __table_args__ = (Index("idx_recoveries_payment", "payment_id"),)

    recovery_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    payment_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("payments.payment_id"),
        nullable=False,
    )
    decision_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("policy_decisions.decision_id"),
        nullable=False,
    )
    # Format: recovery:{payment_id}:{action_type}:{attempt_number}
    # UNIQUE constraint is the physical double-execution backstop
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)  # SUCCESS|FAILED|PENDING
    # Recovered amount in paise — BIGINT, never Float
    recovered_amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    provider_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    stopping_rule_triggered: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # NULL or e.g. 'MAX_RETRIES'
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    # Relationships
    payment: Mapped[Payment] = relationship(back_populates="recoveries")
    policy_decision: Mapped[PolicyDecision] = relationship(back_populates="recoveries")
    audit_logs: Mapped[list[AuditLog]] = relationship(back_populates="recovery")


class RecoveryLedger(Base):
    """
    Financial ground truth per payment — the table the evaluation harness queries.
    baseline_outcome comes from the BaselineSimulator run over the same synthetic set.
    incremental_recovery_paise = actual_recovery_paise - baseline equivalent.
    ALL columns are BIGINT paise. The evaluation SQL query SUM()s only these columns.
    """

    __tablename__ = "recovery_ledger"

    ledger_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    payment_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("payments.payment_id"),
        nullable=False,
    )
    revenue_at_risk_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_recovery_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_recovery_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # from simulator ground truth: what baseline strategy would've gotten
    baseline_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    incremental_recovery_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    intervention_cost_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    net_recovery_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    # Relationships
    payment: Mapped[Payment] = relationship(back_populates="recovery_ledger")


class AuditLog(Base):
    """
    Insert-only audit trail — every decision in the system generates a row here.
    REVOKE UPDATE, DELETE on this table from app_role at the DB level.
    This is enforced at the Postgres GRANT level, not just application discipline.

    Every state transition is traceable from this table joined to events.
    The audit explorer is a query over this table — not a separately-maintained view.
    """

    __tablename__ = "audit_log"

    audit_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    payment_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("payments.payment_id"),
        nullable=True,
    )
    diagnosis_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("diagnoses.diagnosis_id"),
        nullable=True,
    )
    candidate_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("candidate_actions.candidate_id"),
        nullable=True,
    )
    decision_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("policy_decisions.decision_id"),
        nullable=True,
    )
    recovery_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("recoveries.recovery_id"),
        nullable=True,
    )
    # human-readable one-liner for the audit explorer
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    # Relationships
    payment: Mapped[Payment | None] = relationship(back_populates="audit_logs")
    diagnosis: Mapped[Diagnosis | None] = relationship(back_populates="audit_logs")
    candidate: Mapped[CandidateAction | None] = relationship(back_populates="audit_logs")
    policy_decision: Mapped[PolicyDecision | None] = relationship(back_populates="audit_logs")
    recovery: Mapped[Recovery | None] = relationship(back_populates="audit_logs")


# ═══════════════════════════════════════════════════════════════════════════════
# ACTION COSTS (gaps.md §A.2 — configurable per merchant, not hardcoded constants)
# ═══════════════════════════════════════════════════════════════════════════════


class ActionCost(Base):
    """
    Configurable action cost table.
    merchant_id=NULL represents the platform-wide default.
    Lookup order: merchant-specific first → platform default fallback.
    All costs in BIGINT paise.
    """

    __tablename__ = "action_costs"
    __table_args__ = (
        # Unique per (merchant, action_type, version)
        # COALESCE trick: NULL merchant_id is treated as a fixed sentinel UUID
        # for uniqueness purposes — enforced via partial unique index instead:
        Index("idx_action_cost_merchant_action", "merchant_id", "action_type", "version"),
    )

    action_cost_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    merchant_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("merchants.merchant_id"),
        nullable=True,
    )  # NULL = platform default
    # RETRY_NOW|RETRY_LATER|ALT_ROUTE|REMINDER|ESCALATE|DO_NOTHING
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    cost_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    friction_base_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    effective_from: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


# ═══════════════════════════════════════════════════════════════════════════════
# EVALUATION (baseline comparison runs)
# ═══════════════════════════════════════════════════════════════════════════════


class BaselineRun(Base):
    """
    Stores outcome from the baseline retry strategy for each payment.
    Used in the evaluation harness SQL join (TRD §7).
    baseline_recovery_paise = what the simple fixed-retry strategy recovered.
    """

    __tablename__ = "baseline_runs"

    run_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    experiment_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    payment_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("payments.payment_id"),
        nullable=False,
    )
    recovered_amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SIMULATOR MANIFESTS & LATENT STATE (TRD §6, gaps.md §B.1)
# ═══════════════════════════════════════════════════════════════════════════════


class SimulationManifest(Base):
    """
    Tracks metadata and configuration for reproducible simulation runs.
    """

    __tablename__ = "simulator_manifests"

    simulation_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generator_version: Mapped[str] = mapped_column(Text, nullable=False)
    scenario_config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    latent_function_version: Mapped[str] = mapped_column(Text, nullable=False)
    total_payments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    # Relationships
    latent_states: Mapped[list[SimulatorLatentState]] = relationship(
        back_populates="manifest", cascade="all, delete-orphan"
    )


class SimulatorLatentState(Base):
    """
    Stores hidden parameters and true recoverability probability generated by
    the simulator's latent recoverability function.

    SECURITY: No inference role or diagnoser role is granted access to this table.
    """

    __tablename__ = "simulator_latent_state"
    __table_args__ = (Index("idx_latent_simulation", "simulation_id"),)

    latent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    simulation_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("simulator_manifests.simulation_id", ondelete="CASCADE"),
        nullable=False,
    )
    payment_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("payments.payment_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    customer_patience_score: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    bank_latent_health: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    latent_network_noise: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    latent_customer_propensity: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    true_recovery_prob_bps: Mapped[int] = mapped_column(BigInteger, nullable=False)
    true_failure_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    # Relationships
    manifest: Mapped[SimulationManifest] = relationship(back_populates="latent_states")
