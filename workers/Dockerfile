# RecoveryOS Worker — Dockerfile
# Celery worker for executing scheduled recovery jobs.
# Inherits the same Python base as the API image.

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
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

COPY recoveryos/    ./recoveryos/
COPY workers/       ./workers/
COPY services/      ./services/
COPY integrations/  ./integrations/
COPY migrations/    ./migrations/
COPY alembic.ini    ./

RUN useradd -m -u 1001 recoveryos && chown -R recoveryos:recoveryos /app
USER recoveryos

CMD ["celery", "-A", "workers.celery_app", "worker", "--loglevel=info", "-Q", "recovery,celery"]
