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

# pandas + scikit-learn + pyyaml: Task REPLAN1 made workers/retry_scheduler.py
# import services.pipeline.consumer -> services.recovery_engine.orchestrator
# -> services.recovery_engine.propensity, which imports pandas at module
# level and needs scikit-learn to unpickle model_lr.pkl (pyyaml comes from
# that same chain via simulator/calibration/loader.py). This Dockerfile
# never carried them -- same class of gap Task MD1 already fixed in
# apps/api/Dockerfile (see that file's comment), just not mirrored here
# because execution_worker.py alone never reached that import chain.
#
# Versions pinned to match requirements-dev.txt / apps/api/Dockerfile
# exactly -- sklearn's pickle format is version-sensitive, and
# model_lr.pkl/feature_transformer_v1.pkl were pickled with these exact
# versions; an unpinned install pulling a newer major version at build
# time risks failing (or silently differing) at unpickle time.
RUN pip install --no-cache-dir \
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
