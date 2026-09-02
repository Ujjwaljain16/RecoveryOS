"""
Phase 11 -- AI-on/AI-off ablation + tie-break tolerance sensitivity sweep.

Same infrastructure pattern as tests/evaluation/multi_seed_runner.py (full
docker compose down -v -> up -> migrate -> simulator.run(seed) -> publish
every canonical PAYMENT_FAILED event -> wait for full drain -> collect
metrics), extended with docker-compose.override.ai_fusion.yml to control
AI_RECOMMENDATION_FUSION_ENABLED / AI_TIE_BREAK_TOLERANCE_BPS per run.

UNLIKE multi_seed_runner.py's baseline override, this does NOT disable the
LLM -- the ablation needs real RecoveryRecommendation rows to compare fusion
on vs off against, so Gemini stays live (normal `docker compose up`
behavior) for every arm, including the "AI OFF" one: recommendations are
still generated and persisted there, just never consulted by
_apply_ai_fusion (see services/recovery_engine/orchestrator.py). That's
what makes the AI_OFF vs AI_ON_100bps comparison a genuine test of whether
fusion changes outcomes, not a confound of "did the LLM even run."

Four runs, same seed held constant across all of them (only the fusion
config changes) so the comparison isolates fusion's effect:
  1. AI_OFF               -- ai_recommendation_fusion_enabled=false
  2. AI_ON_tol_0           -- enabled, tie_tolerance_bps=0   (exact ties only)
  3. AI_ON_tol_100 (default) -- enabled, tie_tolerance_bps=100 (1%)
  4. AI_ON_tol_500          -- enabled, tie_tolerance_bps=500 (5%)

Per the Phase 11 design doc's explicit requirement: the tolerance must be
fixed BEFORE evaluation and swept, never tuned post-hoc to match a desired
result -- these three values (0/100/500 bps) were chosen when the plan was
written, not after seeing any run's output.

Writes incremental results to
tests/evaluation/artifacts/ai_ablation_results.json after EACH run (same
crash-safety reasoning as multi_seed_runner.py). Run as a background
process -- each run takes roughly as long as one multi_seed_runner.py seed.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import psycopg2
import psycopg2.extras
import redis

REPO_ROOT = Path(r"D:\Projects\Hack\RecoveryOS")
PG_DSN = "postgresql://recoveryos:H8c8oUdrDB397w_TkX-WLo1xQhJEZTwh@localhost:5433/recoveryos"
REDIS_URL = "redis://localhost:6379/0"
RESULTS_FILE = REPO_ROOT / "tests" / "evaluation" / "artifacts" / "ai_ablation_results.json"

SEED = 42  # held constant across every arm -- only the fusion config varies

RUNS = [
    {"name": "AI_OFF", "fusion_enabled": False, "tolerance_bps": 100},
    {"name": "AI_ON_tol_0", "fusion_enabled": True, "tolerance_bps": 0},
    {"name": "AI_ON_tol_100", "fusion_enabled": True, "tolerance_bps": 100},
    {"name": "AI_ON_tol_500", "fusion_enabled": True, "tolerance_bps": 500},
]

COMPOSE = "docker compose -f docker-compose.yml -f docker-compose.override.ai_fusion.yml"


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


def regenerate_dataset(seed: int, fusion_enabled: bool, tolerance_bps: int):
    env = dict(os.environ)
    env["AI_RECOMMENDATION_FUSION_ENABLED"] = "true" if fusion_enabled else "false"
    env["AI_TIE_BREAK_TOLERANCE_BPS"] = str(tolerance_bps)

    run(f"{COMPOSE} down -v", env=env)
    run(f"{COMPOSE} up -d postgres redis", env=env)
    wait_for_postgres_healthy()
    time.sleep(5)
    run("python -m dotenv run -- alembic upgrade head", env=env)
    run(
        f"python -m dotenv run -- python -m simulator.run "
        f'--n=10000 --seed={seed} --customers=2000 --scenario-weights="{{}}" --output=db',
        env=env,
    )
    run(f"{COMPOSE} up -d --build", env=env)
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
        print(
            f"  drain progress: ledger={ledger_count}/{expected_ledger_rows} lag={lag} pending={pending}",
            flush=True,
        )
        if ledger_count >= expected_ledger_rows and lag == 0 and pending == 0:
            conn.close()
            return
        time.sleep(20)
    conn.close()
    raise RuntimeError("pipeline never fully drained within timeout")


def collect_metrics(run_name: str, seed: int, fusion_enabled: bool, tolerance_bps: int, start_wall_time: float) -> dict:
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
    recovered_payments = scalar(
        "SELECT count(*) FROM recovery_ledger WHERE actual_recovery_paise > 0"
    )
    interventions = scalar("SELECT count(*) FROM policy_decisions WHERE verdict='ALLOW'")
    escalates = scalar("SELECT count(*) FROM policy_decisions WHERE verdict='ESCALATE'")
    unnecessary = scalar(
        """
        SELECT count(*) FROM recovery_ledger rl
        JOIN policy_decisions pd ON pd.payment_id = rl.payment_id
        WHERE pd.verdict = 'ALLOW' AND rl.incremental_recovery_paise <= 0
        """
    )
    total_attempts = scalar("SELECT count(*) FROM recoveries")

    # ── The Phase 11-specific decomposition, read from decision_fusion_trace ──
    recommendations_available = scalar(
        "SELECT count(*) FROM decision_fusion_trace WHERE ai_recommended_action IS NOT NULL"
    )
    tie_break_applied = scalar(
        "SELECT count(*) FROM decision_fusion_trace WHERE tie_break_applied = true"
    )
    risk_escalation_applied = scalar(
        "SELECT count(*) FROM decision_fusion_trace WHERE risk_escalation_applied = true"
    )
    tie_break_rejected_outside_tolerance = scalar(
        """
        SELECT count(*) FROM decision_fusion_trace
        WHERE tie_break_applied = false AND risk_escalation_applied = false
          AND ai_recommended_action IS NOT NULL
          AND ai_recommended_action != deterministic_chosen_action
          AND NOT (ai_recommended_action = ANY(
              SELECT (c->>'action_type') FROM jsonb_array_elements(near_tied_candidates) c
          ))
        """
    )
    tie_break_rejected_policy = scalar(
        """
        SELECT count(*) FROM decision_fusion_trace
        WHERE tie_break_applied = false AND risk_escalation_applied = false
          AND ai_recommended_action IS NOT NULL
          AND ai_recommended_action != deterministic_chosen_action
          AND ai_recommended_action = ANY(
              SELECT (c->>'action_type') FROM jsonb_array_elements(near_tied_candidates) c
          )
        """
    )
    # THE headline number: total decisions where the final action differed
    # from what plain deterministic economics would have chosen.
    ai_outcome_delta_total = tie_break_applied + risk_escalation_applied
    # Must always be zero -- no fusion-accepted decision should have a
    # final_action the deterministic economics/policy hadn't already
    # cleared on its own. A nonzero count here means an invariant broke.
    unsafe_deltas = scalar(
        """
        SELECT count(*) FROM decision_fusion_trace dft
        JOIN policy_decisions pd ON pd.decision_id = dft.decision_id
        WHERE dft.tie_break_applied = true AND pd.verdict != 'ALLOW'
        """
    )

    elapsed_seconds = time.time() - start_wall_time
    avg_latency_ms_per_payment = (
        (elapsed_seconds / failed_payments) * 1000 if failed_payments else None
    )

    conn.close()

    return {
        "run_name": run_name,
        "seed": seed,
        "fusion_enabled": fusion_enabled,
        "tolerance_bps": tolerance_bps,
        "failed_payments": failed_payments,
        "recoveryos_total_paise": recoveryos_total,
        "baseline_total_paise": baseline_total,
        "incremental_recovery_paise": recoveryos_total - baseline_total,
        "recovery_rate": recovered_payments / failed_payments if failed_payments else None,
        "intervention_rate": interventions / failed_payments if failed_payments else None,
        "unnecessary_intervention_rate": unnecessary / interventions if interventions else None,
        "escalations": escalates,
        "total_attempts": total_attempts,
        "attempts_per_payment": total_attempts / failed_payments if failed_payments else None,
        "revenue_per_attempt_paise": recoveryos_total / total_attempts if total_attempts else None,
        "ai_recommendations_available": recommendations_available,
        "ai_recommendation_acceptance_rate": (
            ai_outcome_delta_total / recommendations_available if recommendations_available else None
        ),
        "ai_tie_break_applied": tie_break_applied,
        "ai_risk_escalations": risk_escalation_applied,
        "ai_tie_break_rejected_outside_tolerance": tie_break_rejected_outside_tolerance,
        "ai_tie_break_rejected_policy": tie_break_rejected_policy,
        "ai_outcome_delta_total": ai_outcome_delta_total,
        "ai_unsafe_deltas": unsafe_deltas,  # MUST be 0 in every arm
        "wall_clock_seconds_full_run": elapsed_seconds,
        "avg_throughput_latency_ms_per_payment": avg_latency_ms_per_payment,
    }


def main():
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    results = []
    if RESULTS_FILE.exists():
        results = json.loads(RESULTS_FILE.read_text())
        done_runs = {r["run_name"] for r in results}
    else:
        done_runs = set()

    for run_config in RUNS:
        name = run_config["name"]
        if name in done_runs:
            print(f"run={name} already done, skipping")
            continue
        print(f"\n{'=' * 60}\nRUN {name} (fusion_enabled={run_config['fusion_enabled']}, "
              f"tolerance_bps={run_config['tolerance_bps']})\n{'=' * 60}", flush=True)
        start = time.time()
        regenerate_dataset(SEED, run_config["fusion_enabled"], run_config["tolerance_bps"])
        n_events = publish_all_failed_payments()
        print(f"published {n_events} events", flush=True)
        wait_for_drain(expected_ledger_rows=n_events)
        metrics = collect_metrics(
            name, SEED, run_config["fusion_enabled"], run_config["tolerance_bps"], start
        )
        print(json.dumps(metrics, indent=2), flush=True)
        assert metrics["ai_unsafe_deltas"] == 0, (
            f"SAFETY INVARIANT VIOLATED in run={name}: {metrics['ai_unsafe_deltas']} tie-break "
            "decisions were applied despite a non-ALLOW policy verdict -- stop and investigate "
            "before running further arms"
        )
        results.append(metrics)
        RESULTS_FILE.write_text(json.dumps(results, indent=2))
        print(f"run={name} done, results saved to {RESULTS_FILE}", flush=True)

    print("\nALL RUNS COMPLETE")


if __name__ == "__main__":
    main()
