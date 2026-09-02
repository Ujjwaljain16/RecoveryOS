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
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
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
    # created|authorized|failed|success|expired|recovered
    # 'recovered' (Task E1, Phase 8 Scenario 4 fix): set by
    # services/pipeline/ledger.py when a recovery attempt reaches a real
    # SUCCESS outcome -- distinct from 'success', which means the ORIGINAL
    # authorization succeeded on the first attempt and was never failed.
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


class EventPublication(Base):
    """
    Marks that an Event's downstream publish (stream:risk_engine) has
    actually succeeded — deliberately a SEPARATE table from `events`, not a
    column on it (Task S4, pre-Phase-8 audit): `events` has UPDATE/DELETE
    revoked from app_role (migrations/0002_db_roles.py's APPEND_ONLY_TABLES,
    TRD §9's immutability guarantee), so a mutable published_at column on
    that table couldn't ever be written by the role that needs to write it.
    This table is INSERT-only by construction instead — one row per
    event_id, ever, marking "published" as a fact that gets recorded once,
    never a status field that gets flipped.

    Decouples "was this Event row newly inserted" (insert_event_idempotent's
    is_new) from "has it actually been published yet" — before this existed,
    a publish failure after a successful Event commit meant every retry
    found is_new=False and silently skipped the publish forever.
    """

    __tablename__ = "event_publications"

    event_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("events.event_id", ondelete="CASCADE"),
        primary_key=True,
    )
    published_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


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
        # These are rates (0.0–1.0), not money — NUMERIC is appropriate here.
        # Explicit type matches migrations/0001 and TRD §2 exactly (Task R2,
        # pre-Phase-8 audit) -- without it, SQLAlchemy's default type
        # inference for `float` is FLOAT, not NUMERIC, which drifted from
        # the real schema and would confuse a future `alembic
        # revision --autogenerate` into proposing to revert it.
        Numeric(5, 4),
        nullable=True,
    )
    observed_rate: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    z_score: Mapped[float | None] = mapped_column(Numeric(6, 3), nullable=True)
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
        # Dedup per triggering event, not per payment -- a payment can
        # legitimately have multiple diagnoses across multiple real retry
        # attempts. NULL source_event_id (no event context, e.g. direct
        # calls/tests) never collides with anything, by Postgres's normal
        # NULL-is-distinct semantics. migrations/0013, Task S1.
        UniqueConstraint("payment_id", "source_event_id", name="uq_diagnoses_payment_event"),
    )

    diagnosis_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    payment_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("payments.payment_id"),
        nullable=True,
    )
    # The stream:risk_engine message (services/event_processor/publisher.py's
    # source_event_id) that triggered this decision cycle -- NULL if this
    # diagnosis wasn't produced by the pipeline consumer (tests, direct
    # calls). Threaded through by services/pipeline/consumer.py so a
    # redelivered message can't create a duplicate diagnosis for the SAME
    # triggering event (migrations/0013, Task S1).
    source_event_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    cohort_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), nullable=True
    )  # NULL if isolated, set if systemic
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    # 0.0-1.0 (not money) -- explicit Numeric(4,3) matches migrations/0001 and
    # TRD §2 (Task R2, pre-Phase-8 audit; same drift class as AnomalyWindow above).
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    evidence: Mapped[dict] = mapped_column(
        JSONB, nullable=False
    )  # structured facts cited, for grounding checks
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    is_fallback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )  # added per gaps.md §A.3
    # CONFIDENT|LIKELY|AMBIGUOUS|INSUFFICIENT_EVIDENCE|CONFLICTING_SIGNALS|ESCALATE --
    # the investigative diagnoser's honest qualitative confidence (Task AGENT1,
    # agent-design review point 1: don't let a float pretend to be a
    # calibrated probability). NULL for single-call/fallback diagnoses,
    # which only ever produce the numeric `confidence` above.
    confidence_band: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    # Relationships
    payment: Mapped[Payment | None] = relationship(back_populates="diagnoses")
    audit_logs: Mapped[list[AuditLog]] = relationship(back_populates="diagnosis")
    hypotheses: Mapped[list[DiagnosisHypothesis]] = relationship(back_populates="diagnosis")
    investigation_steps: Mapped[list[InvestigationStep]] = relationship(back_populates="diagnosis")


class DiagnosisHypothesis(Base):
    """One candidate root cause considered during an investigative
    diagnosis (Task AGENT1) -- not just the winner. support_score/
    contradict_score/evidence_count are plain integers the investigation
    loop increments as tool results come in, deliberately NOT a
    probability (agent-design review point 1)."""

    __tablename__ = "diagnosis_hypotheses"
    __table_args__ = (Index("idx_diagnosis_hypotheses_diagnosis", "diagnosis_id"),)

    hypothesis_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    diagnosis_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("diagnoses.diagnosis_id"), nullable=False
    )
    cause: Mapped[str] = mapped_column(Text, nullable=False)
    support_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contradict_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unresolved_questions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    is_selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    diagnosis: Mapped[Diagnosis] = relationship(back_populates="hypotheses")


class InvestigationStep(Base):
    """One tool call made during an investigative diagnosis, with the
    InvestigationScore that justified choosing it (Task AGENT1, agent-
    design review points 2/3). expected_uncertainty_reduction is an
    LLM-ESTIMATED score, not true entropy math -- tool_cost/latency_ms
    come from the ToolRegistry's own declared, real constants."""

    __tablename__ = "investigation_steps"
    __table_args__ = (
        Index("idx_investigation_steps_diagnosis", "diagnosis_id"),
        UniqueConstraint("diagnosis_id", "step_number"),
    )

    step_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    diagnosis_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("diagnoses.diagnosis_id"), nullable=False
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    tool_inputs: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tool_output_summary: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    expected_uncertainty_reduction: Mapped[float] = mapped_column(Numeric(6, 3), nullable=False)
    tool_cost: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    investigation_score: Mapped[float] = mapped_column(Numeric(8, 3), nullable=False)
    called_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )

    diagnosis: Mapped[Diagnosis] = relationship(back_populates="investigation_steps")


class RecoveryRecommendation(Base):
    """
    Phase 11 -- the AI investigator's bounded, advisory recovery
    recommendation. Written by services/diagnosis_engine/diagnoser.py's
    persist_investigation, alongside diagnosis_hypotheses/investigation_steps.
    recommended_action is one of the SAME six action_type strings
    candidate_actions already uses -- the recommendation cannot name an
    action the deterministic engine hasn't already scored.
    """

    __tablename__ = "recovery_recommendations"
    __table_args__ = (
        Index("idx_recovery_recommendations_diagnosis", "diagnosis_id"),
        CheckConstraint(
            "recommended_action IN ('RETRY_NOW','RETRY_LATER','ALT_ROUTE','REMINDER',"
            "'ESCALATE','DO_NOTHING')",
            name="ck_recovery_recommendations_action",
        ),
    )

    recommendation_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    diagnosis_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("diagnoses.diagnosis_id"), nullable=False
    )
    payment_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("payments.payment_id"), nullable=True
    )
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_delay_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    risk_flags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    recovery_rationale: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


class DecisionFusionTrace(Base):
    """
    Phase 11 -- persisted provenance for exactly how (or whether) an AI
    recommendation influenced one policy_decision. Written for EVERY
    decision once ai_recommendation_fusion_enabled is on, including
    "no recommendation available"/"fusion disabled" rows -- see
    services/recovery_engine/orchestrator.py and migration 0021's docstring
    for why this is unconditional, not only written on an accepted
    recommendation.
    """

    __tablename__ = "decision_fusion_trace"
    __table_args__ = (Index("idx_decision_fusion_trace_decision", "decision_id"),)

    fusion_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    decision_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("policy_decisions.decision_id"), nullable=False, unique=True
    )
    recommendation_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("recovery_recommendations.recommendation_id"), nullable=True
    )
    deterministic_chosen_action: Mapped[str] = mapped_column(Text, nullable=False)
    deterministic_chosen_evi_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    near_tied_candidates: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    tie_tolerance_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    ai_recommended_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    ai_risk_flags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    tie_break_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    risk_escalation_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    final_action: Mapped[str] = mapped_column(Text, nullable=False)
    fusion_reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


class DiagnosisOutcome(Base):
    """Closes the loop (Task AGENT1, agent-design review point 4): one row
    per diagnosis, written once a terminal outcome exists. diagnosis_correct
    is nullable and ONLY ever populated in the simulator/offline-eval
    context (ground truth via app_role, same as Phase 8's AI-eval) -- a
    real production case has no ground truth to check the diagnosis
    against, only whether the chosen action worked (action_effective)."""

    __tablename__ = "diagnosis_outcomes"

    outcome_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    diagnosis_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("diagnoses.diagnosis_id"), nullable=False, unique=True
    )
    chosen_action: Mapped[str] = mapped_column(Text, nullable=False)
    observed_outcome: Mapped[str] = mapped_column(Text, nullable=False)
    diagnosis_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    action_effective: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    counterfactual_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


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
    __table_args__ = (
        # One row per (payment, triggering event, action_type) -- 6 action
        # types are scored per decision cycle, so action_type must be part
        # of the key. NULL source_event_id never collides (see Diagnosis's
        # equivalent constraint for the full reasoning). migrations/0013,
        # Task S1.
        UniqueConstraint(
            "payment_id",
            "source_event_id",
            "action_type",
            name="uq_candidate_actions_payment_event_action",
        ),
    )

    candidate_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    payment_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("payments.payment_id"),
        nullable=False,
    )
    # Triggering stream:risk_engine message's source_event_id -- see
    # Diagnosis.source_event_id's comment for the full reasoning.
    source_event_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
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
    # The recovery strategist's own confidence that THIS action's expected
    # value will actually be realized -- separate from diagnosis confidence
    # (Task AGENT1: "we're highly confident this is systemic degradation,
    # but not confident retrying immediately has positive expected value"
    # is a real, distinct signal from how sure the diagnosis was). NULL
    # until the recovery strategist is wired to populate it.
    action_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
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
    __table_args__ = (
        # One decision per (payment, triggering event) -- NULL source_event_id
        # never collides (see Diagnosis's equivalent constraint for the full
        # reasoning). migrations/0013, Task S1.
        UniqueConstraint("payment_id", "source_event_id", name="uq_policy_decisions_payment_event"),
    )

    decision_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    payment_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("payments.payment_id"),
        nullable=False,
    )
    # Triggering stream:risk_engine message's source_event_id -- see
    # Diagnosis.source_event_id's comment for the full reasoning.
    source_event_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
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


class RawWebhookEvent(Base):
    """
    Task WEBHOOK1 -- the verbatim record of every inbound Razorpay webhook,
    signature-verified or not (an unverified one is still stored, as
    evidence of a rejected/forged delivery attempt, never silently
    discarded). idempotency_key is Razorpay's own `X-Razorpay-Event-Id`
    header value when present (prefixed `evtid:`), falling back to a
    SHA-256 content hash (prefixed `sha256:`) only when that header is
    genuinely absent -- CORRECTED (Domain Audit finding F4): this used to
    claim Razorpay webhooks carry no unique event-id field at all, which
    was false; see integrations/razorpay/webhooks.py:compute_idempotency_key.
    matched_recovery_id links this event to the
    RazorpayTestAdapter-created order it resolves, via recoveries.provider_ref
    (the real order id) -- reconciliation, not a fresh payment-identity
    mapping (see migration 0016's docstring for the exact scope boundary).
    """

    __tablename__ = "raw_webhook_events"
    __table_args__ = (Index("idx_raw_webhook_events_matched_recovery", "matched_recovery_id"),)

    webhook_event_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    headers: Mapped[dict] = mapped_column(JSONB, nullable=False)
    signature_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    matched_recovery_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("recoveries.recovery_id"), nullable=True
    )
    reconciliation_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)


class ScheduledReevaluation(Base):
    """
    Task REPLAN1 -- the real deferred-execution mechanism for RETRY_LATER.
    A row here means "re-evaluate this payment from scratch at
    scheduled_for" -- NOT "execute this specific action then." Kept
    separate from `recoveries` (see migration 0017's docstring) so a
    not-yet-fired schedule can never be double-counted as an attempt that
    already happened.
    """

    __tablename__ = "scheduled_reevaluations"
    __table_args__ = (
        Index("idx_scheduled_reevaluations_due", "status", "scheduled_for"),
        UniqueConstraint(
            "payment_id", "source_event_id", name="uq_scheduled_reevaluations_payment_event"
        ),
        CheckConstraint(
            "status IN ('PENDING', 'FIRED', 'CANCELLED', 'COMPLETED')",
            name="ck_scheduled_reevaluations_status",
        ),
    )

    reevaluation_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=_uuid
    )
    payment_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("payments.payment_id"), nullable=False
    )
    decision_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("policy_decisions.decision_id"), nullable=False
    )
    diagnosis_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("diagnoses.diagnosis_id"), nullable=True
    )
    source_event_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    scheduled_for: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING")
    claimed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    # Migration 0025 -- adversarial sweep finding #50: a claimed (FIRED) row
    # with no completion path was a permanent orphan on any crash between
    # claim and completion. Set to claimed_at + REEVALUATION_LEASE_SECONDS
    # (services/recovery_engine/scheduling.py) at claim time; a FIRED row
    # whose lease has expired is reclaimable exactly like a fresh PENDING
    # row. NULL for every pre-migration row and for COMPLETED/CANCELLED
    # rows -- never a match for "expired" comparisons.
    lease_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    fired_source_event_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    # Phase 12/13 -- which mission this re-evaluation belongs to, so
    # workers/retry_scheduler.py can REUSE (not recreate) that mission on
    # firing. Nullable: rows written before Phase 12, or by a caller that
    # doesn't track missions, still insert cleanly.
    mission_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("recovery_missions.mission_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


class RecoveryMission(Base):
    """
    Phase 12 -- one row per payment's mission lifecycle: an explicit,
    code-owned state machine wrapping however many investigate -> decide ->
    execute -> observe rounds it takes to reach a terminal state. See
    services/recovery_engine/mission.py for the transition table and budget
    enforcement -- this row's state/current_round/current_attempt columns
    are the ONLY place "what round/attempt are we on" lives; nothing
    upstream (including the AI) writes to them directly.

    Mutable in place (state/current_round/current_attempt/ended_at/
    updated_at) -- the one exception to this system's append-only
    discipline for the same reason ScheduledReevaluation's status/claimed_at
    already are: a mission's CURRENT state is something calling code reads/
    updates atomically, not just a fact reconstructable from mission_events'
    history (which stays genuinely append-only).
    """

    __tablename__ = "recovery_missions"
    __table_args__ = (
        Index("idx_recovery_missions_payment", "payment_id"),
        CheckConstraint(
            "state IN ('OBSERVED','INVESTIGATING','PLANNING','AWAITING_AUTHORIZATION',"
            "'EXECUTING','OBSERVING_OUTCOME','RECOVERED','ESCALATED','TERMINATED')",
            name="ck_recovery_missions_state",
        ),
    )

    mission_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    payment_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("payments.payment_id"), nullable=False
    )
    state: Mapped[str] = mapped_column(Text, nullable=False, default="OBSERVED")
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    max_investigation_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    max_mission_duration_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=604_800
    )
    max_money_exposure_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    current_round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


class MissionEvent(Base):
    """
    Phase 12 -- the append-only, ordered trace of everything that happened
    to a RecoveryMission. This IS the "open one payment, see its entire
    autonomous trajectory" artifact -- one row per meaningful transition
    (PAYMENT_FAILED, MISSION_CREATED, INVESTIGATION_STARTED,
    HYPOTHESIS_UPDATED, AI_RECOMMENDATION, POLICY_AUTHORIZED,
    ACTION_EXECUTING, RECOVERY_SUCCEEDED/FAILED, REINVESTIGATION_STARTED,
    MISSION_RECOVERED/ESCALATED/TERMINATED), each attributed to a real actor
    (system|ai|policy_engine|execution_worker), never mutated after
    insert -- same discipline as audit_log/events (migration 0002 REVOKEs
    UPDATE, DELETE from app_role on those; this table follows the same
    intent even though the grant itself is only SELECT/INSERT here).
    """

    __tablename__ = "mission_events"
    __table_args__ = (
        Index("idx_mission_events_mission", "mission_id", "sequence_number"),
        UniqueConstraint("mission_id", "sequence_number", name="uq_mission_events_sequence"),
    )

    event_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    mission_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("recovery_missions.mission_id"), nullable=False
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


class RecoveryLedger(Base):
    """
    Financial ground truth per payment — the table the evaluation harness queries.
    baseline_outcome comes from the BaselineSimulator run over the same synthetic set.
    incremental_recovery_paise = actual_recovery_paise - baseline equivalent.
    ALL columns are BIGINT paise. The evaluation SQL query SUM()s only these columns.
    """

    __tablename__ = "recovery_ledger"
    __table_args__ = (
        # One terminal ledger entry per payment (a payment reaches ONE
        # terminal state) -- the physical backstop against a redelivered
        # pipeline message double-writing this table and inflating TRD §7's
        # SUM()-based incremental-revenue number. migrations/0013, Task S1.
        UniqueConstraint("payment_id", name="uq_recovery_ledger_payment"),
    )

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
    # gaps.md sec:A.1 (migration 0023) -- a customer-level action (e.g. an
    # opt-out) has no payment to anchor to; events.payment_id is NOT NULL so
    # events can't hold it either. Nullable, same pattern as every other FK
    # on this table.
    customer_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("customers.customer_id"),
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
    customer: Mapped[Customer | None] = relationship()


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
        # Unique per (merchant, action_type, version). NULL merchant_id (the
        # platform default) is folded to a sentinel UUID via COALESCE so
        # Postgres's NULL-is-distinct semantics don't allow duplicate
        # platform-default rows — migrations/0010_action_costs_unique_constraint.py.
        Index(
            "uq_action_cost_merchant_action",
            text("COALESCE(merchant_id, '00000000-0000-0000-0000-000000000000')"),
            "action_type",
            "version",
            unique=True,
        ),
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
    # Domain Audit finding #6 (migration 0018): how many simulated attempts
    # a FAIR (same attempt budget as RecoveryOS) baseline run consumed
    # before success/exhaustion -- NULL for the original single-attempt
    # baseline runs, which have no concept of "attempts used" beyond 1.
    attempts_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Migration 0024: which policy_engine rule (if any) stopped a
    # compliance-aware baseline run short of max_retries/success -- NULL for
    # the single-attempt baseline and the compliance-blind fair baseline,
    # neither of which evaluate the compliance-rule chain at all.
    blocked_by_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
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
