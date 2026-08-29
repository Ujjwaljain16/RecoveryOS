"""
Health check router.

Task A3: this used to return a static {"status": "ok", ...} regardless of
whether Postgres or Redis were actually reachable — a judge or an on-call
engineer trusting this during a live demo would see green while the stack
was actually broken. Now runs a real SELECT 1 and a real PING and reports
503 with which dependency failed if either does.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from recoveryos.config import get_settings
from recoveryos.database import get_app_engine, get_app_session
from recoveryos.metrics import (
    incremental_revenue_paise_reconciled,
    recovery_attempts_reconciled,
    recovery_success_reconciled,
    revenue_at_risk_paise_reconciled,
    revenue_recovered_paise_reconciled,
)
from recoveryos.redis import get_redis_pool

logger = logging.getLogger(__name__)

router = APIRouter()


async def _reconcile_from_ledger(session: AsyncSession) -> None:
    """
    Production Architecture Domain Audit finding #1: recompute the
    headline business gauges from the durable source of truth on every
    scrape -- see recoveryos/metrics.py's own module comment for why this
    exists (in-process Counters reset to 0 on any worker restart with no
    reconciliation). Best-effort: a transient DB hiccup here must not
    break the whole /metrics scrape (Prometheus itself, and every OTHER
    series on this endpoint, must keep working even if this one query
    fails) -- caught and logged, not raised.
    """
    try:
        ledger_row = (
            (
                await session.execute(
                    text(
                        "SELECT "
                        "COALESCE(SUM(revenue_at_risk_paise), 0) AS revenue_at_risk, "
                        "COALESCE(SUM(actual_recovery_paise), 0) AS recovered, "
                        "COALESCE(SUM(incremental_recovery_paise), 0) AS incremental "
                        "FROM recovery_ledger"
                    )
                )
            )
            .mappings()
            .one()
        )
        revenue_at_risk_paise_reconciled.set(int(ledger_row["revenue_at_risk"]))
        revenue_recovered_paise_reconciled.set(int(ledger_row["recovered"]))
        incremental_revenue_paise_reconciled.set(int(ledger_row["incremental"]))

        attempt_rows = (
            (
                await session.execute(
                    text(
                        "SELECT action_type, "
                        "COUNT(*) AS attempts, "
                        "COUNT(*) FILTER (WHERE outcome = 'SUCCESS') AS successes "
                        "FROM recoveries WHERE outcome IS NOT NULL GROUP BY action_type"
                    )
                )
            )
            .mappings()
            .all()
        )
        for row in attempt_rows:
            recovery_attempts_reconciled.labels(action_type=row["action_type"]).set(row["attempts"])
            recovery_success_reconciled.labels(action_type=row["action_type"]).set(row["successes"])
    except Exception:
        logger.exception("[Metrics] failed to reconcile gauges from recovery_ledger (non-fatal)")


@router.get("/metrics", summary="Prometheus scrape endpoint (TRD §10)")
async def metrics(session: AsyncSession = Depends(get_app_session)) -> Response:
    """
    Unauthenticated by design, same as every other /metrics endpoint
    Prometheus scrapes (it never sends X-API-Key) -- this process's own
    prometheus_client default REGISTRY, which recoveryos/metrics.py's
    Counters/Histogram are registered on by every module in this process
    that imports them (apps/api itself only records anomaly-related series
    via /v1/simulate/degrade; the bulk of TRD §10's series are recorded by
    whichever of pipeline_orchestrator/execution_worker/retry_scheduler
    actually did the work -- see monitoring/prometheus/prometheus.yml for
    the other scrape jobs). ALSO recomputes the reconciled business gauges
    (see _reconcile_from_ledger) fresh from recovery_ledger/recoveries on
    every single scrape -- this is the one process every scrape hits, so
    it's the natural place for a live-DB-truth reconciliation pass,
    regardless of which OTHER process actually did the underlying work.
    """
    await _reconcile_from_ledger(session)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/health", summary="Health check")
async def health(response: Response):
    settings = get_settings()

    checks: dict[str, str] = {}
    healthy = True

    try:
        engine = get_app_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        healthy = False
        checks["database"] = f"unreachable: {type(exc).__name__}: {exc}"

    try:
        redis_client = get_redis_pool()
        await redis_client.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        healthy = False
        checks["redis"] = f"unreachable: {type(exc).__name__}: {exc}"

    response.status_code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if healthy else "unhealthy",
        "env": settings.env.value,
        "service": "recoveryos-api",
        "checks": checks,
    }
