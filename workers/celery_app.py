"""
RecoveryOS — Celery Worker Application
=========================================
Manages async recovery job execution.

Architecture constraint (TRD §1.3, §4.3):
  - Workers execute via PaymentProviderAdapter, NOT the LLM.
  - Every job has an idempotency_key = recovery:{payment_id}:{action_type}:{attempt_number}
  - Advisory lock acquired BEFORE existence check (gaps.md §B.2 — lock-before-check pattern).
  - Duplicate INSERT into recoveries is physically prevented by the UNIQUE constraint on
    idempotency_key (DB-level backstop independent of the lock logic).
"""

from __future__ import annotations

import logging

from celery import Celery
from celery.signals import worker_ready

from recoveryos.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

celery_app = Celery(
    "recoveryos",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Retry failed tasks with exponential backoff (up to 3 attempts at worker level)
    task_acks_late=True,  # ACK only after successful execution — prevents silent loss
    task_reject_on_worker_lost=True,  # Re-queue if worker crashes mid-task
    worker_prefetch_multiplier=1,  # One task at a time per worker — safer for financial ops
    task_track_started=True,
    # Dead-letter: tasks failing after all retries go to a dedicated queue
    task_routes={
        "workers.tasks.execute_recovery": {"queue": "recovery"},
    },
)


@worker_ready.connect
def on_worker_ready(sender, **kwargs):
    logger.info("RecoveryOS worker ready. Broker: %s", settings.celery_broker_url)
