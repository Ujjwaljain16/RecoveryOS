"""
Investigation tool registry -- Task AGENT1, point 2 of the agent-design
review: the investigative diagnoser chooses from an EXPLICIT capability
registry, not an arbitrary toolbox. Every tool declares its own cost and
latency (real, fixed constants -- not estimated at call time) so
InvestigationScore (services/diagnosis_engine/investigator.py) can combine
a real cost/latency penalty with the LLM's own estimated benefit for that
tool, rather than everything being guesswork.

Same role boundary as build_diagnosis_input() (diagnoser.py): every tool
here runs on a diagnoser_role session. DIAGNOSER_SAFE_TABLES (migrations/
0002) already covers every table these tools touch -- no new grants were
needed for this file. Every query uses an explicit column allow-list, the
same discipline diagnoser.py's own _PAYMENT_SAFE_COLUMNS already follows
(gaps.md SB.1: no wildcard "SELECT star" in any inference-reachable path).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ToolSpec:
    name: str
    purpose: str
    tool_cost: float  # abstract cost units (DB round-trips, roughly) -- a real, fixed constant
    latency_ms_estimate: int
    allowed_for_diagnosis: bool = True


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "get_customer_payment_history": ToolSpec(
        name="get_customer_payment_history",
        purpose="Recent payments for this customer -- distinguishes a customer-specific "
        "problem from a one-off failure.",
        tool_cost=1.0,
        latency_ms_estimate=15,
    ),
    "get_customer_recovery_history": ToolSpec(
        name="get_customer_recovery_history",
        purpose="Past recovery attempts and their outcomes for this customer -- surfaces "
        "whether retrying has worked for them before.",
        tool_cost=1.0,
        latency_ms_estimate=15,
    ),
    "get_cohort_failure_rate": ToolSpec(
        name="get_cohort_failure_rate",
        purpose="Current failure rate for this bank+method cohort vs. its own recent "
        "baseline -- the strongest single signal for distinguishing customer-specific "
        "from systemic causes.",
        tool_cost=1.5,
        latency_ms_estimate=25,
    ),
    "get_recent_anomalies": ToolSpec(
        name="get_recent_anomalies",
        purpose="Recently detected anomaly windows for this bank/method -- confirms or "
        "contradicts a systemic-degradation hypothesis against the risk engine's own "
        "independent z-score detector.",
        tool_cost=1.0,
        latency_ms_estimate=15,
    ),
    "get_payment_attempt_history": ToolSpec(
        name="get_payment_attempt_history",
        purpose="Prior recovery attempts made specifically on THIS payment -- relevant for "
        "repeated-failure/escalation reasoning.",
        tool_cost=0.5,
        latency_ms_estimate=10,
    ),
    "get_intervention_history": ToolSpec(
        name="get_intervention_history",
        purpose="Prior policy decisions made on THIS payment (allowed/blocked, and why) -- "
        "avoids the investigator re-deriving a conclusion the policy engine already reached.",
        tool_cost=0.5,
        latency_ms_estimate=10,
    ),
}


async def get_customer_payment_history(
    session: AsyncSession, payment_id: str, limit: int = 10
) -> list[dict]:
    """
    Takes payment_id, not customer_id -- resolves the owning customer
    server-side and never surfaces the customer_id itself to the caller
    (the LLM investigator never sees it, same PII-minimization principle
    DiagnosisInput's own docstring states: "even an opaque id is more
    identity than this layer needs").
    """
    rows = (
        (
            await session.execute(
                text(
                    "SELECT payment_id, amount_paise, method, bank, status, failure_code, "
                    "created_at FROM payments WHERE customer_id = "
                    "(SELECT customer_id FROM payments WHERE payment_id = :pid) "
                    "ORDER BY created_at DESC LIMIT :limit"
                ),
                {"pid": payment_id, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def get_customer_recovery_history(
    session: AsyncSession, payment_id: str, limit: int = 10
) -> list[dict]:
    """Same payment_id-in, customer_id-never-surfaced pattern as
    get_customer_payment_history()."""
    rows = (
        (
            await session.execute(
                text(
                    "SELECT r.recovery_id, r.payment_id, r.action_type, r.outcome, "
                    "r.attempt_number, r.executed_at FROM recoveries r "
                    "JOIN payments p ON p.payment_id = r.payment_id "
                    "WHERE p.customer_id = (SELECT customer_id FROM payments WHERE payment_id = :pid) "
                    "ORDER BY r.executed_at DESC LIMIT :limit"
                ),
                {"pid": payment_id, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def get_cohort_failure_rate(
    session: AsyncSession, bank: str | None, method: str, window_minutes: int = 60
) -> dict:
    if bank is None:
        return {"bank": None, "note": "no bank on this payment -- cohort scoped to method only"}
    since = datetime.now(UTC) - timedelta(minutes=window_minutes)
    baseline_since = since - timedelta(days=7)
    row = (
        (
            await session.execute(
                text(
                    "SELECT count(*) FILTER (WHERE status = 'failed') AS failed, "
                    "count(*) AS total FROM payments "
                    "WHERE bank = :bank AND method = :method AND created_at >= :since"
                ),
                {"bank": bank, "method": method, "since": since},
            )
        )
        .mappings()
        .first()
    )
    baseline_row = (
        (
            await session.execute(
                text(
                    "SELECT count(*) FILTER (WHERE status = 'failed') AS failed, "
                    "count(*) AS total FROM payments "
                    "WHERE bank = :bank AND method = :method "
                    "AND created_at >= :baseline_since AND created_at < :since"
                ),
                {"bank": bank, "method": method, "baseline_since": baseline_since, "since": since},
            )
        )
        .mappings()
        .first()
    )
    current_rate = (row["failed"] / row["total"]) if row["total"] else None
    baseline_rate = (
        (baseline_row["failed"] / baseline_row["total"]) if baseline_row["total"] else None
    )
    return {
        "bank": bank,
        "method": method,
        "window_minutes": window_minutes,
        "current_failure_rate": current_rate,
        "current_sample_size": row["total"],
        "baseline_failure_rate": baseline_rate,
        "baseline_sample_size": baseline_row["total"],
    }


async def get_recent_anomalies(
    session: AsyncSession, bank: str | None, method: str, window_minutes: int = 120
) -> list[dict]:
    # method is unused here -- every TOOL_REGISTRY tool shares the same
    # (bank, method) calling convention (investigator.py's generic
    # argument derivation), but anomaly_windows only tracks bank-scoped
    # anomalies for this lookup, never method-scoped ones.
    if bank is None:
        return []
    since = datetime.now(UTC) - timedelta(minutes=window_minutes)
    rows = (
        (
            await session.execute(
                text(
                    "SELECT scope_type, scope_entity, time_bucket, severity, is_anomaly, "
                    "z_score, observed_rate, baseline_rate FROM anomaly_windows "
                    "WHERE scope_type = 'bank' AND scope_entity = :bank AND time_bucket >= :since "
                    "ORDER BY time_bucket DESC"
                ),
                {"bank": bank, "since": since},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def get_payment_attempt_history(session: AsyncSession, payment_id: str) -> list[dict]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT recovery_id, action_type, attempt_number, outcome, executed_at "
                    "FROM recoveries WHERE payment_id = :pid ORDER BY attempt_number ASC"
                ),
                {"pid": payment_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def get_intervention_history(session: AsyncSession, payment_id: str) -> list[dict]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT decision_id, verdict, rule_trace, created_at "
                    "FROM policy_decisions WHERE payment_id = :pid ORDER BY created_at ASC"
                ),
                {"pid": payment_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


_TOOL_IMPLEMENTATIONS = {
    "get_customer_payment_history": get_customer_payment_history,
    "get_customer_recovery_history": get_customer_recovery_history,
    "get_cohort_failure_rate": get_cohort_failure_rate,
    "get_recent_anomalies": get_recent_anomalies,
    "get_payment_attempt_history": get_payment_attempt_history,
    "get_intervention_history": get_intervention_history,
}


async def call_tool(session: AsyncSession, name: str, **kwargs) -> list | dict:
    """Dispatch to the named tool's real implementation. Raises KeyError for
    an unregistered name -- the investigator must only ever propose names
    from TOOL_REGISTRY, checked before this is called."""
    if name not in TOOL_REGISTRY:
        raise KeyError(f"unknown tool: {name!r}")
    return await _TOOL_IMPLEMENTATIONS[name](session, **kwargs)
