"""
Health check router.

Task A3: this used to return a static {"status": "ok", ...} regardless of
whether Postgres or Redis were actually reachable — a judge or an on-call
engineer trusting this during a live demo would see green while the stack
was actually broken. Now runs a real SELECT 1 and a real PING and reports
503 with which dependency failed if either does.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from recoveryos.config import get_settings
from recoveryos.database import get_app_engine
from recoveryos.redis import get_redis_pool

router = APIRouter()


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
