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
#
# Versions pinned to match requirements-dev.txt exactly -- an unpinned
# scikit-learn/pandas here is a real risk, not just a style nit: sklearn's
# pickle format is version-sensitive, and model_lr.pkl/feature_transformer_v1.pkl
# were pickled with the exact scikit-learn/pandas versions below. An
# unpinned `pip install` pulling a newer major version at build time could
# fail to unpickle them, or unpickle silently-differently, at runtime.
RUN pip install --no-cache-dir \
    fastapi==0.115.0 \
    "uvicorn[standard]"==0.30.0 \
    pydantic==2.12.4 \
    pydantic-settings==2.5.0 \
    "sqlalchemy[asyncio]"==2.0.35 \
    asyncpg==0.31.0 \
    psycopg2-binary==2.9.9 \
    alembic==1.16.5 \
    redis==8.1.0 \
    prometheus-client==0.26.0 \
    httpx==0.28.1 \
    structlog==25.5.0 \
    pandas==2.2.0 \
    scikit-learn==1.7.2 \
    pyyaml==6.0.2

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
# Just the artifact GET /v1/experiments/phase8-baseline serves (not the
# whole tests/ tree, which pulls in test-only fixtures/deps this runtime
# image has no other reason to carry).
COPY tests/evaluation/artifacts/ ./tests/evaluation/artifacts/

RUN useradd -m -u 1001 recoveryos && chown -R recoveryos:recoveryos /app
USER recoveryos

EXPOSE 8000

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
