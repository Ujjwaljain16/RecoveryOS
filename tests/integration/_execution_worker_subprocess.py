"""
Standalone subprocess entry point for Phase 6's crash-recovery tests.

Not a pytest test file itself (leading underscore keeps pytest from
collecting it) — this is the actual child process
test_worker_crash_recovery() launches via subprocess.Popen and kills mid-job.
It must be a real, separate OS process (not a thread) so a real kill
genuinely terminates the DB connection holding the advisory lock, rather
than just cancelling an in-process coroutine.

CountingSimulatorAdapter wraps the real SimulatorAdapter but ALSO writes an
unconditional (no ON CONFLICT, no dedup) row to a scratch table on every
single `.retry()` invocation — independent of the idempotency machinery
being tested, so if that machinery were broken and the provider really did
get called twice, this table would show it. This is the cross-process-
visible call counter the crash-recovery test reads back afterward.
"""

from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import Connection  # noqa: E402

from integrations.razorpay.adapter import ProviderResult, SimulatorAdapter  # noqa: E402


class CountingSimulatorAdapter:
    def __init__(self, call_log_table: str):
        self._inner = SimulatorAdapter()
        self._call_log_table = call_log_table

    def retry(
        self, conn: Connection, payment_id: str, amount_paise: int, attempt_number: int
    ) -> ProviderResult:
        # Unconditional insert — no ON CONFLICT, no dedup. If process_job's
        # idempotency wrapper is broken and calls this twice, two rows
        # appear here, full stop.
        conn.execute(
            text(f"INSERT INTO {self._call_log_table} (call_id, payment_id) VALUES (:cid, :pid)"),
            {"cid": str(uuid.uuid4()), "pid": payment_id},
        )
        conn.commit()
        return self._inner.retry(conn, payment_id, amount_paise, attempt_number)


def main() -> None:
    import logging

    import redis as sync_redis

    from recoveryos.config import get_settings
    from workers.execution_worker import run_worker

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    get_settings.cache_clear()
    call_log_table = os.environ["TEST_CALL_LOG_TABLE"]
    max_iterations = int(os.environ.get("TEST_MAX_ITERATIONS", "1"))

    settings = get_settings()
    redis_client = sync_redis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    provider = CountingSimulatorAdapter(call_log_table)
    try:
        run_worker(redis_client, max_iterations=max_iterations, provider=provider)
    finally:
        redis_client.close()


if __name__ == "__main__":
    main()
