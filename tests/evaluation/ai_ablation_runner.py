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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import redis

REPO_ROOT = Path(r"D:\Projects\Hack\RecoveryOS")
PG_DSN = "postgresql://recoveryos:H8c8oUdrDB397w_TkX-WLo1xQhJEZTwh@localhost:5433/recoveryos"
REDIS_URL = "redis://localhost:6379/0"
RESULTS_FILE = REPO_ROOT / "tests" / "evaluation" / "artifacts" / "ai_ablation_results.json"

SEED = 42  # held constant across every arm -- only the fusion config varies

# Same reasoning as tests/evaluation/multi_seed_runner.py's EVALUATION_START_TIME
# -- and same EVALUATION_START_TIME_ISO env override, so this script shares the
# exact same instant as multi_seed_runner.py and the standalone canonical run
# within one evaluation campaign, not just internally across its own 4 arms.
_env_start_time = os.environ.get("EVALUATION_START_TIME_ISO")
EVALUATION_START_TIME = (
    datetime.fromisoformat(_env_start_time)
    if _env_start_time
    else datetime.now(timezone.utc) - timedelta(hours=1)
)

# Reduced-scale mode: the free-tier Gemini quota (per key, per model) is
# small (confirmed live: GenerateRequestsPerDayPerProjectPerModel-FreeTier,
# limit=20/day) and every diagnosis -- even the AI_OFF arm, which still
# generates real recommendations, just never consults them -- costs up to
# MAX_INVESTIGATION_ROUNDS=2 real LLM calls. ABLATION_N_PAYMENTS lets a
# quota-constrained run shrink the population instead of exhausting the key
# partway through. Defaults preserve the original full-scale design.
#
# NOTE: a per-arm GEMINI_API_KEY/AI_DIAGNOSER_GEMINI_MODEL override was
# attempted here and DOES NOT WORK -- docker-compose.yml's
# pipeline_orchestrator/execution_worker services load these two vars via
# `env_file: .env` (reads the FILE on disk), not from the shell environment
# that invokes `docker compose`, unlike AI_RECOMMENDATION_FUSION_ENABLED/
# AI_TIE_BREAK_TOLERANCE_BPS below, which docker-compose.override.ai_fusion.yml
# explicitly substitutes via `${VAR}` and which therefore DO work per-arm.
# All 4 arms always use whatever GEMINI_API_KEY/AI_DIAGNOSER_GEMINI_MODEL is
# literally written in .env -- there is no way to vary it per arm without
# either editing .env between arms or changing the compose file to add an
# explicit `${GEMINI_API_KEY}` substitution.
N_PAYMENTS = int(os.environ.get("ABLATION_N_PAYMENTS", "10000"))

RUNS = [
    {"name": "AI_OFF", "fusion_enabled": False, "tolerance_bps": 100},
    {"name": "AI_ON_tol_0", "fusion_enabled": True, "tolerance_bps": 0},
    {"name": "AI_ON_tol_100", "fusion_enabled": True, "tolerance_bps": 100},
    {"name": "AI_ON_tol_500", "fusion_enabled": True, "tolerance_bps": 500},
]

COMPOSE = "docker compose -f docker-compose.yml -f docker-compose.override.ai_fusion.yml"

# gaps.md sec:C.5 -- see tests/evaluation/multi_seed_runner.py's identical
# constant/function for the full rationale. Same fix, same reasoning, applied
# here too so the ablation's own AI-OFF/AI-ON arms compare RecoveryOS's real
# attempt-2 behavior fairly, not just its attempt-1 behavior.
PLATFORM_DEFAULT_POLICY_CONFIG_ID = "00000000-0000-0000-0000-000000000001"


def accelerate_evaluation_cooldown():
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO policy_configs (policy_config_id, retry_cooldown_hours)
        VALUES (%s, 0)
        ON CONFLICT (policy_config_id) DO UPDATE SET retry_cooldown_hours = 0
        """,
        (PLATFORM_DEFAULT_POLICY_CONFIG_ID,),
    )
    conn.commit()
    conn.close()


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


def wait_for_reevaluations_drained(timeout=600, stable_polls_required=3):
    """See tests/evaluation/multi_seed_runner.py's identical function for
    the full rationale (race between round-1 ledger draining and round-2
    scheduling)."""
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    deadline = time.time() + timeout
    last_total = None
    stable_count = 0
    while time.time() < deadline:
        cur.execute("SELECT count(*) FROM scheduled_reevaluations WHERE status = 'PENDING'")
        pending = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM scheduled_reevaluations")
        total = cur.fetchone()[0]
        print(f"  round-2 (rescheduled) drain progress: pending={pending}/{total} scheduled", flush=True)
        if pending == 0:
            stable_count = stable_count + 1 if total == last_total else 1
            last_total = total
            if stable_count >= stable_polls_required:
                conn.close()
                return total
        else:
            stable_count = 0
            last_total = total
        time.sleep(5)
    conn.close()
    raise RuntimeError("scheduled_reevaluations never fully drained within timeout")


def regenerate_dataset(seed: int, fusion_enabled: bool, tolerance_bps: int):
    env = dict(os.environ)
    env["AI_RECOMMENDATION_FUSION_ENABLED"] = "true" if fusion_enabled else "false"
    env["AI_TIE_BREAK_TOLERANCE_BPS"] = str(tolerance_bps)

    run(f"{COMPOSE} down -v", env=env)
    run(f"{COMPOSE} up -d postgres redis", env=env)
    wait_for_postgres_healthy()
    time.sleep(5)
    run("python -m dotenv run -- alembic upgrade head", env=env)
    accelerate_evaluation_cooldown()
    run(
        f"python -m dotenv run -- python -m simulator.run "
        f'--n={N_PAYMENTS} --seed={seed} --customers=2000 --scenario-weights="{{}}" '
        f'--start-time={EVALUATION_START_TIME.isoformat()} --output=db',
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
    # gaps.md sec:C.5 -- RENAMED from unnecessary_intervention_rate; same
    # query, honestly relabeled. See multi_seed_runner.py's identical
    # computation for the full semantic-correction rationale: this counts
    # ALLOW decisions that did not strictly beat a naive single-attempt
    # retry (baseline_total below is always the single-attempt baseline,
    # never the fair one) -- including ties where both succeeded.
    did_not_beat_single_attempt_baseline = scalar(
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
        "sim_start_time": EVALUATION_START_TIME.isoformat(),
        "fusion_enabled": fusion_enabled,
        "tolerance_bps": tolerance_bps,
        "failed_payments": failed_payments,
        "recoveryos_total_paise": recoveryos_total,
        "baseline_total_paise": baseline_total,
        "incremental_recovery_paise": recoveryos_total - baseline_total,
        "recovery_rate": recovered_payments / failed_payments if failed_payments else None,
        "intervention_rate": interventions / failed_payments if failed_payments else None,
        "did_not_beat_single_attempt_baseline_rate": (
            did_not_beat_single_attempt_baseline / interventions if interventions else None
        ),
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
    print(f"sim_start_time (shared across all 4 arms this run): {EVALUATION_START_TIME.isoformat()}", flush=True)
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
        regenerate_dataset(
            SEED,
            run_config["fusion_enabled"],
            run_config["tolerance_bps"],
        )
        n_events = publish_all_failed_payments()
        print(f"published {n_events} events", flush=True)
        wait_for_drain(expected_ledger_rows=n_events)
        wait_for_reevaluations_drained()
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
