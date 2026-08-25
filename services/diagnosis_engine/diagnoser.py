"""
Diagnosis orchestrator.

Role boundary (verify by connection string, not comment):
  - Reading payment features happens on get_diagnoser_session_factory()
    (the `diagnoser` login user, diagnoser_role's grants) —
    ground_truth_recoverable is not even in the SELECT list here, and even
    if it were, the column-level GRANT (migrations/0002) would reject it.
  - Persisting the resulting Diagnosis row happens on
    get_app_session_factory() (app_role) — diagnoser_role has ZERO INSERT
    grant anywhere (confirmed by test_diagnoser_role_has_no_write_access),
    so this split isn't a style choice, it's the only way a diagnosis ever
    gets written at all.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from recoveryos.database import get_app_session_factory, get_diagnoser_session_factory
from recoveryos.models import Diagnosis
from services.diagnosis_engine.fallback_rules import diagnose_fallback
from services.diagnosis_engine.llm_diagnoser import diagnose_with_llm
from services.diagnosis_engine.schemas import DiagnosisInput, DiagnosisOutput, RootCause
from services.risk_engine.anomaly import derive_cohort_id

logger = logging.getLogger(__name__)

# 12 safe columns from migrations/0002 DIAGNOSER_PAYMENT_COLUMNS. A
# wildcard select-all is banned in any inference-reachable path (gaps.md
# §B.1) — this explicit allow-list IS the enforcement, not just
# documentation of intent: even if someone widened the DB grant by mistake
# later, this query still only asks for these columns.
_PAYMENT_SAFE_COLUMNS = (
    "payment_id, merchant_id, customer_id, amount_paise, method, bank, "
    "status, failure_code, failure_class, is_synthetic, created_at, failed_at"
)


async def build_diagnosis_input(
    diagnoser_session: AsyncSession, payment_id: str
) -> DiagnosisInput | None:
    """
    Fetch everything the diagnoser is allowed to see for one payment, on the
    diagnoser_role connection. Returns None if the payment doesn't exist.
    """
    row = (
        (
            await diagnoser_session.execute(
                text(f"SELECT {_PAYMENT_SAFE_COLUMNS} FROM payments WHERE payment_id = :pid"),
                {"pid": payment_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None

    customer_row = (
        (
            await diagnoser_session.execute(
                text("SELECT is_returning FROM customers WHERE customer_id = :cid"),
                {"cid": row["customer_id"]},
            )
        )
        .mappings()
        .first()
    )

    anomaly_row = None
    if row["bank"]:
        anomaly_row = (
            (
                await diagnoser_session.execute(
                    text(
                        """
                    SELECT scope_type, scope_entity, time_bucket, z_score, severity,
                           observed_rate, baseline_rate, is_anomaly
                    FROM anomaly_windows
                    WHERE scope_type = 'bank' AND scope_entity = :bank
                    ORDER BY time_bucket DESC LIMIT 1
                    """
                    ),
                    {"bank": row["bank"]},
                )
            )
            .mappings()
            .first()
        )

    return DiagnosisInput(
        payment_id=str(row["payment_id"]),
        amount_paise=row["amount_paise"],
        method=row["method"],
        bank=row["bank"],
        failure_code=row["failure_code"],
        failure_class=row["failure_class"],
        customer_is_returning=customer_row["is_returning"] if customer_row else None,
        is_anomaly=bool(anomaly_row["is_anomaly"]) if anomaly_row else False,
        anomaly_severity=anomaly_row["severity"] if anomaly_row else None,
        anomaly_scope_type=anomaly_row["scope_type"] if anomaly_row else None,
        anomaly_scope_entity=anomaly_row["scope_entity"] if anomaly_row else None,
        anomaly_z_score=(
            float(anomaly_row["z_score"])
            if anomaly_row and anomaly_row["z_score"] is not None
            else None
        ),
        anomaly_observed_rate=(
            float(anomaly_row["observed_rate"])
            if anomaly_row and anomaly_row["observed_rate"] is not None
            else None
        ),
        anomaly_baseline_rate=(
            float(anomaly_row["baseline_rate"])
            if anomaly_row and anomaly_row["baseline_rate"] is not None
            else None
        ),
        anomaly_time_bucket=anomaly_row["time_bucket"] if anomaly_row else None,
    )


def _attach_cohort_if_systemic(
    diagnosis_input: DiagnosisInput, output: DiagnosisOutput
) -> DiagnosisOutput:
    """
    Uniform post-processing applied to EITHER path's output: if this
    payment sits in an active high-severity systemic anomaly window, this
    IS a cohort diagnosis — regardless of whether the LLM or the fallback
    table produced the initial guess. Idempotent against the fallback path
    (which never sets cohort_id itself) and safe against an LLM output that
    already independently concluded systemic_degradation (it just gets the
    matching cohort_id attached to it).
    """
    if not (diagnosis_input.is_anomaly and diagnosis_input.anomaly_severity == "high"):
        return output
    if output.root_cause == RootCause.CONFLICTING_SIGNALS:
        # An active contradiction was already flagged by the adversarial
        # guard — that verdict takes precedence over "just label it
        # systemic", so leave it alone.
        return output

    cohort_id = derive_cohort_id(
        diagnosis_input.anomaly_scope_type,
        diagnosis_input.anomaly_scope_entity,
        diagnosis_input.anomaly_time_bucket,
    )
    return output.model_copy(
        update={"root_cause": RootCause.SYSTEMIC_DEGRADATION, "cohort_id": cohort_id}
    )


async def diagnose(payment_id: str) -> DiagnosisOutput | None:
    """
    Full diagnosis pipeline for one payment: read (diagnoser_role) -> LLM
    attempt with hard timeout -> deterministic fallback if needed -> uniform
    cohort attachment. Does NOT persist — diagnoser_role can't write
    anywhere, so persistence is always a separate app_role step
    (persist_diagnosis).
    """
    async with get_diagnoser_session_factory()() as diagnoser_session:
        diagnosis_input = await build_diagnosis_input(diagnoser_session, payment_id)

    if diagnosis_input is None:
        logger.warning(
            "[Diagnoser] payment_id=%s not found (or not visible under diagnoser_role)", payment_id
        )
        return None

    output, failure_reason = await diagnose_with_llm(diagnosis_input)
    if output is None:
        output = diagnose_fallback(diagnosis_input, reason=failure_reason)

    return _attach_cohort_if_systemic(diagnosis_input, output)


async def persist_diagnosis(
    app_session: AsyncSession, payment_id: str, output: DiagnosisOutput
) -> Diagnosis:
    """
    Write the Diagnosis row. MUST run on an app_role session —
    diagnoser_role has zero INSERT grant on `diagnoses` (or anywhere else),
    confirmed by test_diagnoser_role_has_no_write_access. This is the only
    function in the whole diagnosis pipeline that writes anything.
    """
    diagnosis = Diagnosis(
        diagnosis_id=str(uuid.uuid4()),
        payment_id=payment_id,
        cohort_id=output.cohort_id,
        root_cause=output.root_cause.value,
        confidence=output.confidence,
        evidence=[e.model_dump() for e in output.evidence],
        model_version=output.model_version,
        is_fallback=output.is_fallback,
    )
    app_session.add(diagnosis)
    await app_session.commit()
    return diagnosis


async def diagnose_and_persist(payment_id: str) -> Diagnosis | None:
    """Convenience entry point: full pipeline + persistence in one call —
    what a real Risk Engine consumer would call per failed payment."""
    output = await diagnose(payment_id)
    if output is None:
        return None
    async with get_app_session_factory()() as app_session:
        return await persist_diagnosis(app_session, payment_id, output)
