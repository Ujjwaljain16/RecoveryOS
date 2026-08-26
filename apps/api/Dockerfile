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

# pandas + scikit-learn: services/recovery_engine/propensity.py imports pandas
# at module level and needs scikit-learn to unpickle model_lr.pkl/
# feature_transformer_v1.pkl (pickle needs the actual sklearn classes
# available to reconstruct them). apps/api/main.py's response-header
# middleware calls get_current_model_version() on every request, which
# reaches this import chain unconditionally — without these, every request
# 500s, not just the propensity-scoring path. pyyaml: that same import chain
# (propensity.py -> simulator/episodes/generator.py -> .../payments/
# distributions.py -> simulator/calibration/loader.py) reaches a bare
# `import yaml`. Same class of gap Task MD1 fixed in CI's pip install lists
# — this Dockerfile was never updated to match.
RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    pydantic \
    pydantic-settings \
    "sqlalchemy[asyncio]" \
    asyncpg \
    psycopg2-binary \
    alembic \
    redis \
    prometheus-client \
    httpx \
    structlog \
    openai \
    pandas \
    scikit-learn \
    pyyaml

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
