# RecoveryOS API — Dockerfile
# Multi-stage build: deps layer cached separately from app code layer.
# Runs as non-root user (uid 1001) for security.

FROM python:3.11-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Dependency layer (cached unless pyproject.toml changes) ──────────────────
FROM base AS deps

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    pydantic \
    pydantic-settings \
    "sqlalchemy[asyncio]" \
    asyncpg \
    psycopg2-binary \
    alembic \
    "celery[redis]" \
    redis \
    prometheus-client \
    httpx \
    structlog \
    openai

# ── Runtime layer ─────────────────────────────────────────────────────────────
FROM base AS runtime

COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

COPY recoveryos/   ./recoveryos/
COPY apps/         ./apps/
COPY workers/      ./workers/
COPY services/     ./services/
COPY models/       ./models/
COPY simulator/    ./simulator/
COPY integrations/ ./integrations/
COPY migrations/   ./migrations/
COPY alembic.ini   ./

RUN useradd -m -u 1001 recoveryos && chown -R recoveryos:recoveryos /app
USER recoveryos

EXPOSE 8000

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
