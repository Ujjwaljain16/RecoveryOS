"""
RecoveryOS — FastAPI Application
===================================
Entry point for the API service. This module wires together:
  - Lifespan (DB engine startup/teardown)
  - Routers (one per service domain)
  - Middleware (CORS, request-id injection)
  - Health check endpoint

Architectural constraint: this process runs as app_role.
The AI Diagnoser runs in a separate process with diagnoser_role.
No LLM calls happen inside this module.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from recoveryos.config import get_settings
from recoveryos.database import get_app_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup: verify DB and Redis connectivity.
    Shutdown: dispose async engine (closes connection pool).

    Task A3: this used to only check Postgres — a broken Redis connection
    was only discovered on the first request that needed it (rate
    limiting, event ingest), not at boot. Checking both here means a
    misconfigured/unreachable Redis fails loudly at startup instead of
    surfacing as a mysterious 500 on the first real request.
    """
    engine = get_app_engine()
    # Warm-up: execute a trivial query to surface misconfiguration early
    from sqlalchemy import text

    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    from recoveryos.redis import get_redis_pool

    redis_client = get_redis_pool()
    await redis_client.ping()

    yield  # Application runs here

    await engine.dispose()

    from recoveryos.redis import close_redis_pool

    await close_redis_pool()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.api_title,
        version=settings.api_version,
        description=(
            "RecoveryOS — AI Revenue Recovery Control Plane. "
            "Detect → Diagnose → Predict → Optimize → Policy-check → Execute → Measure."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # ─── CORS ─────────────────────────────────────────────────────────────────
    # Task A4: was hardcoded to the Next.js dev server literal — read from
    # config/env instead (still defaults to localhost:3000 for dev).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip() for origin in settings.cors_allowed_origins.split(",") if origin.strip()
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ─── Request-ID middleware ─────────────────────────────────────────────────
    @app.middleware("http")
    async def inject_request_id(request: Request, call_next):
        from apps.api.versioning import get_current_model_version, get_current_policy_version

        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        # Real values, not hardcoded literals — TRD §5's stated purpose is
        # "reproducible against the exact model/policy that produced it,"
        # which a static string can never satisfy. See apps/api/versioning.py.
        response.headers["X-Model-Version"] = get_current_model_version()
        response.headers["X-Policy-Version"] = await get_current_policy_version()
        return response

    # ─── Routes ───────────────────────────────────────────────────────────────
    from apps.api.routers import (
        audit,
        events,
        experiments,
        health,
        incidents,
        payments,
        razorpay_webhooks,
        risk,
        simulate,
    )

    app.include_router(health.router, tags=["Health"])
    app.include_router(events.router, prefix="/v1/events", tags=["Events"])
    app.include_router(risk.router, prefix="/v1/risk", tags=["Risk"])
    app.include_router(payments.router, prefix="/v1/payments", tags=["Payments"])
    app.include_router(audit.router, prefix="/v1/audit", tags=["Audit"])
    app.include_router(experiments.router, prefix="/v1/experiments", tags=["Evaluation"])
    app.include_router(incidents.router, prefix="/v1/incidents", tags=["Incidents"])
    app.include_router(
        razorpay_webhooks.router, prefix="/webhooks/razorpay", tags=["Webhooks"]
    )

    # /v1/simulate only enabled in demo mode
    if settings.is_demo:
        app.include_router(simulate.router, prefix="/v1/simulate", tags=["Simulation"])

    return app


app = create_app()
