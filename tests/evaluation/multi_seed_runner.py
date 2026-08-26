"""
Priority 0 -- multi-seed replication of the Phase 8 canonical run.

For each seed: full docker compose down -v (genuinely empty DB/Redis) ->
up -> migrate -> simulator.run(seed) -> publish every canonical
PAYMENT_FAILED event through the real live pipeline (same approach as
Phase 8's original run) -> wait for full drain -> record every Priority-0
metric -> move to the next seed.

Writes incremental results to tests/evaluation/artifacts/multi_seed_results.json
after EACH seed (so a crash partway through doesn't lose completed seeds).

Run as a background process -- a single seed takes ~10-12 minutes, this
script runs several sequentially.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras
import redis

REPO_ROOT = Path(r"D:\Projects\Hack\RecoveryOS")
PG_DSN = "postgresql://recoveryos:H8c8oUdrDB397w_TkX-WLo1xQhJEZTwh@localhost:5433/recoveryos"
REDIS_URL = "redis://localhost:6379/0"
RESULTS_FILE = REPO_ROOT / "tests" / "evaluation" / "artifacts" / "multi_seed_results.json"

SEEDS = [1, 2, 3, 4, 5]  # + the existing seed=42 run recorded separately


def run(cmd, **kw):
    print(f"$ {cmd}", flush=True)
    return subprocess.run(cmd, shell=True, cwd=REPO_ROOT, check=True, **kw)


def wait_for_postgres_healthy(timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = psycopg2.connect(PG_DSN, connect_timeout=3)
            conn.close()
            return
        except Exception:
            time.sleep(2)
    raise RuntimeError("postgres never became reachable")


COMPOSE = (
    "docker compose -f docker-compose.yml -f docker-compose.override.baseline.yml"
)


def regenerate_dataset(seed: int):
    # Baseline override pins the diagnoser to a guaranteed-invalid key
    # (see docker-compose.override.baseline.yml's docstring) -- LLM
    # availability must not be a confound in a seed-variance study.
    run(f"{COMPOSE} down -v")
    run(f"{COMPOSE} up -d postgres redis")
    wait_for_postgres_healthy()
    time.sleep(5)
    run("python -m dotenv run -- alembic upgrade head")
    run(
        f"python -m dotenv run -- python -m simulator.run "
        f"--n=10000 --seed={seed} --customers=2000 --scenario-weights=\"{{}}\" --output=db"
    )
    run(f"{COMPOSE} up -d --build")
    time.sleep(15)


def publish_all_failed_payments() -> int:
    conn = psycopg2.connect(PG_DSN, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.event_id, e.idempotency_key, p.payment_id, p.merchant_id, p.customer_id,
               p.amount_paise, p.method, p.bank, p.failure_code
        FROM events e JOIN payments p ON p.payment_id = e.payment_id
        WHERE e.event_type = 'PAYMENT_FAILED'
        ORDER BY e.occurred_at
        """
    )
    rows = cur.fetchall()
    conn.close()

    r = redis.from_url(REDIS_URL)
    for row in rows:
        msg = {
            "event_id": str(row["event_id"]),
            "idempotency_key": row["idempotency_key"],
            "payment_id": str(row["payment_id"]),
            "merchant_id": str(row["merchant_id"]),
            "customer_id": str(row["customer_id"]),
            "amount_paise": str(row["amount_paise"]),
            "method": row["method"],
            "bank": row["bank"] or "",
            "event_type": "PAYMENT_FAILED",
            "failure_code": row["failure_code"] or "",
        }
        r.xadd("stream:payment_failed", msg)
    return len(rows)


def wait_for_drain(expected_ledger_rows: int, timeout=1800):
    r = redis.from_url(REDIS_URL)
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = r.xinfo_groups("stream:risk_engine")
            lag = info[0].get("lag") if info else None
            pending = info[0].get("pending") if info else None
        except Exception:
            lag, pending = None, None
        cur.execute("SELECT count(*) FROM recovery_ledger")
        ledger_count = cur.fetchone()[0]
        print(f"  drain progress: ledger={ledger_count}/{expected_ledger_rows} lag={lag} pending={pending}", flush=True)
        if ledger_count >= expected_ledger_rows and lag == 0 and pending == 0:
            conn.close()
            return
        time.sleep(20)
    conn.close()
    raise RuntimeError("pipeline never fully drained within timeout")


def collect_metrics(seed: int, start_wall_time: float) -> dict:
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()

    def scalar(q, params=None):
        cur.execute(q, params or ())
        row = cur.fetchone()
        val = row[0] if row else None
        return int(val) if val is not None else None

    failed_payments = scalar("SELECT count(*) FROM payments WHERE status IN ('failed','recovered')")
    recoveryos_total = scalar("SELECT COALESCE(SUM(actual_recovery_paise),0) FROM recovery_ledger")
    baseline_total = scalar("SELECT COALESCE(SUM(recovered_amount_paise),0) FROM baseline_runs")
    recovered_payments = scalar("SELECT count(*) FROM recovery_ledger WHERE actual_recovery_paise > 0")
    revenue_at_risk = scalar("SELECT COALESCE(SUM(revenue_at_risk_paise),0) FROM recovery_ledger")
    interventions = scalar("SELECT count(*) FROM policy_decisions WHERE verdict='ALLOW'")
    blocks = scalar("SELECT count(*) FROM policy_decisions WHERE verdict='BLOCK'")
    escalates = scalar("SELECT count(*) FROM policy_decisions WHERE verdict='ESCALATE'")
    unnecessary = scalar(
        """
        SELECT count(*) FROM recovery_ledger rl
        JOIN policy_decisions pd ON pd.payment_id = rl.payment_id
        WHERE pd.verdict = 'ALLOW' AND rl.incremental_recovery_paise <= 0
        """
    )
    diagnoses_total = scalar("SELECT count(*) FROM diagnoses")
    abstentions = scalar("SELECT count(*) FROM diagnoses WHERE root_cause = 'unknown'")

    TRUE_TO_EXPECTED_ROOT_CAUSE = {
        "PERMANENT_INVALID_CREDS": "permanent_failure",
        "PERMANENT_EXPIRED_INSTRUMENT": "permanent_failure",
        "PERMANENT_ACCOUNT_CLOSED": "permanent_failure",
        "CUSTOMER_INSUFFICIENT_FUNDS": "customer_specific",
        "BANK_DEGRADATION_FAIL": "temporary_bank_degradation",
        "MULTI_RAIL_OUTAGE_FAIL": "systemic_degradation",
        "TEMPORARY_GATEWAY_TIMEOUT": "temporary_bank_degradation",
        "TRANSIENT_NETWORK_DROP": "temporary_bank_degradation",
    }
    cur.execute(
        """
        SELECT l.true_failure_type, d.root_cause FROM diagnoses d
        JOIN simulator_latent_state l ON l.payment_id = d.payment_id
        """
    )
    root_cause_rows = cur.fetchall()
    committed = [
        (tft, rc) for tft, rc in root_cause_rows if rc != "unknown"
    ]
    correct_committed = sum(
        1 for tft, rc in committed if TRUE_TO_EXPECTED_ROOT_CAUSE.get(tft) == rc
    )

    elapsed_seconds = time.time() - start_wall_time
    avg_latency_ms_per_payment = (elapsed_seconds / failed_payments) * 1000 if failed_payments else None

    conn.close()

    return {
        "seed": seed,
        "model": "logistic_regression (model_lr.pkl, gaps.md SC.2 -- LR beat LightGBM on clean temporal holdout)",
        "failed_payments": failed_payments,
        "recoveryos_total_paise": recoveryos_total,
        "baseline_total_paise": baseline_total,
        "incremental_recovery_paise": recoveryos_total - baseline_total,
        "revenue_at_risk_paise": revenue_at_risk,
        "recovery_rate": recovered_payments / failed_payments if failed_payments else None,
        "revenue_recovery_rate": recoveryos_total / revenue_at_risk if revenue_at_risk else None,
        "intervention_rate": interventions / failed_payments if failed_payments else None,
        "unnecessary_intervention_rate": unnecessary / interventions if interventions else None,
        "policy_blocks": blocks,
        "policy_escalates": escalates,
        "root_cause_accuracy_raw": correct_committed / diagnoses_total if diagnoses_total else None,
        "root_cause_accuracy_committed_only": correct_committed / len(committed) if committed else None,
        "abstention_rate": abstentions / diagnoses_total if diagnoses_total else None,
        "wall_clock_seconds_full_run": elapsed_seconds,
        "avg_throughput_latency_ms_per_payment": avg_latency_ms_per_payment,
    }


def main():
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    results = []
    if RESULTS_FILE.exists():
        results = json.loads(RESULTS_FILE.read_text())
        done_seeds = {r["seed"] for r in results}
    else:
        done_seeds = set()

    for seed in SEEDS:
        if seed in done_seeds:
            print(f"seed={seed} already done, skipping")
            continue
        print(f"\n{'='*60}\nSEED {seed}\n{'='*60}", flush=True)
        start = time.time()
        regenerate_dataset(seed)
        n_events = publish_all_failed_payments()
        print(f"published {n_events} events", flush=True)
        wait_for_drain(expected_ledger_rows=n_events)
        metrics = collect_metrics(seed, start)
        print(json.dumps(metrics, indent=2), flush=True)
        results.append(metrics)
        RESULTS_FILE.write_text(json.dumps(results, indent=2))
        print(f"seed={seed} done, results saved to {RESULTS_FILE}", flush=True)

    print("\nALL SEEDS COMPLETE")


if __name__ == "__main__":
    main()
