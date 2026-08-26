# RecoveryOS Worker — Dockerfile
# Execution worker: Redis Streams consumer for recovery jobs (workers/execution_worker.py).
# Inherits the same Python base as the API image.
#
# Task W1 (pre-Phase-8 audit): the Celery-based task queue this Dockerfile
# used to run (workers/celery_app.py + workers/tasks.py) was a superseded
# implementation attempt -- TRD listed Celery as one of two options for the
# action queue, and the team built a Redis Streams consumer instead
# (matching execution_worker.py's proven advisory-lock idempotency pattern).
# Nothing ever called the Celery task, but its worker container ran anyway
# via docker-compose.yml's `worker` service. Both the Celery files and that
# service are now removed; this Dockerfile's default CMD matches the one
# real thing left in workers/ instead of a stale default that happened to
# never actually run (docker-compose.yml's execution_worker service already
# overrides CMD explicitly, but a correct default avoids the same
# "looks live, isn't" confusion for anyone building this image directly).

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
    redis \
    prometheus-client \
    httpx \
    structlog \
    openai

COPY recoveryos/    ./recoveryos/
COPY workers/       ./workers/
COPY services/      ./services/
COPY models/        ./models/
COPY simulator/     ./simulator/
COPY integrations/  ./integrations/
COPY migrations/    ./migrations/
COPY alembic.ini    ./

RUN useradd -m -u 1001 recoveryos && chown -R recoveryos:recoveryos /app
USER recoveryos

CMD ["python", "-m", "workers.execution_worker"]
