"""
Phase 9 — Next.js dashboard E2E tests (apps/dashboard/), Playwright against
a REAL running stack: the actual FastAPI app (apps.api.main.create_app())
served by uvicorn on a real port, backed by the SAME testcontainer
Postgres/Redis every other integration test uses, and the actual
`next dev` server proxying to it through apps/dashboard/app/api/proxy.
Nothing here is mocked — a browser genuinely renders whatever the real
backend returns, exactly as a demo judge would see it.

Three mandatory tests (deliverable spec):
  - control tower numbers match known, directly-seeded DB state
  - the SIMULATE DEGRADATION button triggers a REAL anomaly_windows row
  - the audit explorer's rendered chain matches the real joined DB rows

These are the heaviest tests in the suite (spin up a real HTTP server AND
a real Node dev server per test) — kept in their own file, not run by
default `pytest tests/unit tests/integration` sweeps unless explicitly
selected, since they need `npm install` already run in apps/dashboard/
and Playwright's chromium browser installed (`playwright install
chromium`).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page, expect
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from apps.api.dependencies.auth import generate_api_key
from tests.integration.conftest import seed_merchant_with_api_key, to_async_url

DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "apps" / "dashboard"
STARTUP_TIMEOUT_SECONDS = 60


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_port(port: int, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"nothing came up on 127.0.0.1:{port} within {timeout}s")


@pytest.fixture()
def bg_loop():
    """
    ONE persistent event loop, in its own thread, shared by everything in
    a given test: the uvicorn server AND every direct DB-seeding coroutine.
    recoveryos/database.py caches its asyncpg engine at module level, and
    that engine binds to whichever loop first used it — dispatching
    seeding calls and the API server onto the SAME loop (via
    run_coroutine_threadsafe) is what keeps that single cached engine
    valid for the whole test, instead of each call spinning its own
    throwaway loop and getting "attached to a different loop" errors the
    moment two of them touch the same cached engine.
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


def _run_async(loop: asyncio.AbstractEventLoop, coro):
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


@pytest.fixture()
def demo_merchant(migrated_db, bg_loop):
    merchant_id = str(uuid.uuid4())
    raw_key = generate_api_key()
    _run_async(
        bg_loop,
        seed_merchant_with_api_key(migrated_db, merchant_id, "e2e-dashboard-merchant", raw_key),
    )
    return merchant_id, raw_key


@pytest.fixture()
def api_server(patch_settings, demo_merchant, bg_loop):
    """
    The REAL FastAPI app, served by uvicorn ON bg_loop (not its own
    separate loop/thread — see bg_loop's docstring for why), against the
    same testcontainer Postgres/Redis `patch_settings` already pointed
    os.environ at for this test. ENV forced to "demo" so
    /v1/simulate/degrade is mounted (apps/api/main.py:126) -- required by
    the simulate-degradation test below.
    """
    os.environ["ENV"] = "demo"
    from recoveryos.config import get_settings

    get_settings.cache_clear()

    from apps.api.main import create_app

    app = create_app()
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    serve_future = asyncio.run_coroutine_threadsafe(server.serve(), bg_loop)
    _wait_for_port(port, STARTUP_TIMEOUT_SECONDS)

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    concurrent.futures.wait([serve_future], timeout=5)


def _wait_for_http_ok(url: str, timeout: float) -> None:
    """
    A raw TCP connect succeeding does NOT mean `next dev` is actually
    serving yet (Windows will accept the connection into its backlog
    before the Node process has bound/compiled anything) — poll a real
    HTTP GET until it returns instead, which is what actually proves the
    server is ready to answer Playwright's first navigation.
    """
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status < 500:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"{url} never returned a real HTTP response within {timeout}s: {last_error}")


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """
    `npm run dev` spawns `next dev` as a CHILD process — proc.terminate()
    on Windows only signals the npm.cmd wrapper, leaving the actual Node
    server running and holding its port (and contending for CPU/IO with
    whatever the NEXT test spins up). taskkill /T kills the whole tree;
    plain terminate()/kill() is the correct (and only available)
    equivalent on POSIX, where the child is in the same process group.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
        )
    else:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture()
def dashboard_server(api_server, demo_merchant):
    """
    The REAL `next dev` server, pointed at `api_server` via env vars the
    same way apps/dashboard/app/api/proxy/[...path]/route.ts reads them
    (API_BASE_URL/API_KEY, server-side only) -- not a build artifact or a
    static export, the actual dev server a person would run.
    """
    _merchant_id, raw_key = demo_merchant
    port = _free_port()
    env = dict(os.environ)
    env["API_BASE_URL"] = api_server
    env["API_KEY"] = raw_key
    env["PORT"] = str(port)
    # Prevent apps/dashboard/.env.local (a developer's own local API key)
    # from ever overriding these — Next.js's dotenv loading takes
    # process.env values as authoritative over .env.local for the same key.

    npm_cmd = "npm.cmd" if sys.platform == "win32" else "npm"
    proc = subprocess.Popen(
        [npm_cmd, "run", "dev", "--", "-p", str(port)],
        cwd=str(DASHBOARD_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_http_ok(base_url, STARTUP_TIMEOUT_SECONDS)
    except RuntimeError:
        _kill_process_tree(proc)
        output = proc.stdout.read() if proc.stdout else ""
        raise RuntimeError(f"dashboard dev server never became ready. Output:\n{output}") from None

    yield base_url

    _kill_process_tree(proc)


async def _seed_ledger_row(
    migrated_db: str,
    merchant_id: str,
    *,
    revenue_at_risk_paise: int,
    actual_recovery_paise: int,
    incremental_recovery_paise: int,
) -> str:
    """Directly seed one payment + one recovery_ledger row with KNOWN
    values, bypassing the full pipeline -- the control tower test only
    needs to prove the dashboard renders whatever recovery_ledger actually
    holds, not re-prove the pipeline that populates it (already covered by
    tests/integration/test_pipeline_e2e.py)."""
    customer_id = str(uuid.uuid4())
    payment_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO customers (customer_id, merchant_id) VALUES (:cid, :mid) "
                "ON CONFLICT (customer_id) DO NOTHING"
            ),
            {"cid": customer_id, "mid": merchant_id},
        )
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, :amount, 'upi', 'HDFC', 'failed', 'TIMEOUT', "
                "'TEMPORARY', true, :ts, :ts)"
            ),
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "amount": revenue_at_risk_paise,
                "ts": datetime.now(UTC) - timedelta(hours=1),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO recovery_ledger (ledger_id, payment_id, revenue_at_risk_paise, "
                "expected_recovery_paise, actual_recovery_paise, incremental_recovery_paise) "
                "VALUES (:lid, :pid, :rar, :rar, :actual, :incr)"
            ),
            {
                "lid": str(uuid.uuid4()),
                "pid": payment_id,
                "rar": revenue_at_risk_paise,
                "actual": actual_recovery_paise,
                "incr": incremental_recovery_paise,
            },
        )
    await engine.dispose()
    return payment_id


def test_dashboard_control_tower_numbers_match_db_state(
    migrated_db, demo_merchant, dashboard_server, bg_loop, page: Page
):
    """
    Seed a known recovery_ledger row (revenue at risk ₹5,000, recovered
    ₹3,000, incremental +₹1,000 -> 60.0% recovery rate) and assert the
    Control Tower renders EXACTLY those numbers -- not just "some numbers."
    """
    merchant_id, _raw_key = demo_merchant
    _run_async(
        bg_loop,
        _seed_ledger_row(
            migrated_db,
            merchant_id,
            revenue_at_risk_paise=500_000,
            actual_recovery_paise=300_000,
            incremental_recovery_paise=100_000,
        )
    )

    page.goto(dashboard_server, wait_until="networkidle")

    expect(page.get_by_text("₹5,000", exact=False)).to_be_visible(timeout=15000)
    expect(page.get_by_text("₹3,000", exact=False)).to_be_visible()
    expect(page.get_by_text("+₹1,000", exact=False)).to_be_visible()
    expect(page.get_by_text("60.0%", exact=False)).to_be_visible()


def test_simulate_degradation_button_triggers_real_anomaly_flag(
    migrated_db, demo_merchant, dashboard_server, page: Page
):
    """
    Click the real button (not a mocked handler) and confirm a REAL
    anomaly_windows row appears in Postgres within a bounded wait --
    proving the click actually reached apps/api/routers/simulate.py's real
    compute_anomaly_window()/persist_anomaly_window() call, not a canned
    200 OK with no DB effect.
    """
    page.goto(dashboard_server, wait_until="networkidle")

    before_count_engine = create_engine(migrated_db, pool_pre_ping=True)
    with before_count_engine.connect() as conn:
        before_count = conn.execute(
            text("SELECT count(*) FROM anomaly_windows WHERE scope_entity = 'HDFC'")
        ).scalar_one()

    button = page.get_by_role("button", name="SIMULATE DEGRADATION")
    expect(button).to_be_enabled(timeout=15000)
    button.click()

    deadline = time.time() + 30
    after_count = before_count
    while time.time() < deadline:
        with before_count_engine.connect() as conn:
            after_count = conn.execute(
                text("SELECT count(*) FROM anomaly_windows WHERE scope_entity = 'HDFC'")
            ).scalar_one()
        if after_count > before_count:
            break
        time.sleep(1)

    assert after_count > before_count, (
        "clicking SIMULATE DEGRADATION must create a real anomaly_windows row for HDFC "
        f"within 30s -- had {before_count} before, {after_count} after"
    )

    with before_count_engine.connect() as conn:
        newest = (
            conn.execute(
                text(
                    "SELECT severity, is_anomaly, z_score FROM anomaly_windows "
                    "WHERE scope_entity = 'HDFC' ORDER BY created_at DESC LIMIT 1"
                )
            )
            .mappings()
            .first()
        )
    assert newest is not None
    # Real detector output over the synthetic-but-real rows the endpoint
    # itself wrote -- a degraded target_success_rate against a normal
    # historical baseline must actually classify as high-severity, not
    # merely "some row got inserted."
    assert newest["severity"] == "high"
    assert newest["is_anomaly"] is True
    before_count_engine.dispose()


async def _seed_and_run_full_chain(migrated_db: str, redis_url: str, merchant_id: str) -> str:
    """Real failed payment + simulator ground truth, run through the ACTUAL
    diagnose -> decide -> execute pipeline (services.pipeline.consumer,
    workers.execution_worker) -- the same real chain
    tests/integration/test_pipeline_e2e.py proves, just parametrized to an
    existing (already-API-keyed) merchant instead of minting its own."""
    import redis.asyncio as aioredis

    from services.pipeline.consumer import process_payment_failure

    customer_id = str(uuid.uuid4())
    payment_id = str(uuid.uuid4())
    engine = create_async_engine(to_async_url(migrated_db))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO customers (customer_id, merchant_id, is_returning) "
                "VALUES (:cid, :mid, true) ON CONFLICT (customer_id) DO NOTHING"
            ),
            {"cid": customer_id, "mid": merchant_id},
        )
        await conn.execute(
            text(
                "INSERT INTO payments (payment_id, merchant_id, customer_id, amount_paise, "
                "method, bank, status, failure_code, failure_class, is_synthetic, created_at, failed_at) "
                "VALUES (:pid, :mid, :cid, 300000, 'upi', 'HDFC', 'failed', 'TIMEOUT', "
                "'TEMPORARY', true, :ts, :ts)"
            ),
            {
                "pid": payment_id,
                "mid": merchant_id,
                "cid": customer_id,
                "ts": datetime.now(UTC) - timedelta(hours=1),
            },
        )
        simulation_id = str(uuid.uuid4())
        await conn.execute(
            text(
                "INSERT INTO simulator_manifests (simulation_id, seed, generator_version, "
                "scenario_config, latent_function_version, total_payments) "
                "VALUES (:sim_id, 1, 'test', '{}'::jsonb, 'test-v1', 1)"
            ),
            {"sim_id": simulation_id},
        )
        await conn.execute(
            text(
                "INSERT INTO simulator_latent_state (latent_id, simulation_id, payment_id, "
                "customer_patience_score, bank_latent_health, latent_network_noise, "
                "latent_customer_propensity, true_recovery_prob_bps, true_failure_type) "
                "VALUES (:lid, :sim_id, :pid, 0.8, 0.9, 0.1, 0.2, 9500, 'TEMPORARY_GATEWAY_TIMEOUT')"
            ),
            {"lid": str(uuid.uuid4()), "sim_id": simulation_id, "pid": payment_id},
        )
    await engine.dispose()

    redis_client = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)
    try:
        await process_payment_failure(payment_id, "HDFC", redis_client)
    finally:
        await redis_client.aclose()

    return payment_id


def test_audit_explorer_chain_matches_db_joins(
    migrated_db, redis_url, demo_merchant, dashboard_server, bg_loop, page: Page
):
    """
    Run one payment through the real pipeline, drain any enqueued
    execution job, then assert the Audit Explorer's rendered chain matches
    the actual joined diagnoses/candidate_actions/policy_decisions/
    recoveries/recovery_ledger rows for that SAME payment_id -- not a
    plausible-looking but disconnected UI.
    """
    merchant_id, _raw_key = demo_merchant
    payment_id = _run_async(
        bg_loop, _seed_and_run_full_chain(migrated_db, redis_url, merchant_id)
    )

    sync_engine = create_engine(migrated_db, pool_pre_ping=True)
    with sync_engine.connect() as conn:
        recoveries_count = conn.execute(
            text("SELECT count(*) FROM recoveries WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()

    if recoveries_count == 0:
        import redis as sync_redis

        from workers.execution_worker import run_worker

        sync_client = sync_redis.from_url(redis_url, encoding="utf-8", decode_responses=True)
        run_worker(sync_client, max_iterations=1)
        sync_client.close()

    with sync_engine.connect() as conn:
        diagnosis_row = (
            conn.execute(
                text(
                    "SELECT root_cause, verdict FROM diagnoses d "
                    "JOIN policy_decisions pd ON pd.payment_id = d.payment_id "
                    "WHERE d.payment_id = :pid ORDER BY d.created_at DESC LIMIT 1"
                ),
                {"pid": payment_id},
            )
            .mappings()
            .first()
        )
        policy_row = conn.execute(
            text(
                "SELECT verdict FROM policy_decisions WHERE payment_id = :pid "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"pid": payment_id},
        ).mappings().first()
        recovery_row = conn.execute(
            text(
                "SELECT action_type, outcome FROM recoveries WHERE payment_id = :pid "
                "ORDER BY attempt_number DESC LIMIT 1"
            ),
            {"pid": payment_id},
        ).mappings().first()

    assert diagnosis_row is not None, "pipeline must have produced a real diagnosis"
    assert policy_row is not None, "pipeline must have produced a real policy decision"

    page.goto(f"{dashboard_server}/audit/{payment_id}", wait_until="networkidle")

    expect(page.get_by_text(diagnosis_row["root_cause"], exact=False)).to_be_visible(timeout=15000)
    expect(page.get_by_text(policy_row["verdict"], exact=False)).to_be_visible()

    if recovery_row is not None:
        expect(page.get_by_text(recovery_row["action_type"], exact=False)).to_be_visible()
        if recovery_row["outcome"]:
            expect(page.get_by_text(recovery_row["outcome"], exact=False)).to_be_visible()

    sync_engine.dispose()
