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
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras
import redis

REPO_ROOT = Path(r"D:\Projects\Hack\RecoveryOS")
PG_DSN = "postgresql://recoveryos:H8c8oUdrDB397w_TkX-WLo1xQhJEZTwh@localhost:5433/recoveryos"
REDIS_URL = "redis://localhost:6379/0"
RESULTS_FILE = REPO_ROOT / "tests" / "evaluation" / "artifacts" / "multi_seed_results.json"

SEEDS = [1, 2, 3, 4, 5]  # + the existing seed=42 run recorded separately

# Every synthetic payment's failed_at is derived from simulator.run's
# --start-time by ticking a deterministic clock forward -- services/
# recovery_engine/orchestrator.py's EligibilityRule blocks anything more
# than 7 real days old. For a full evaluation campaign (this script +
# ai_ablation_runner.py + the standalone canonical run they're compared
# against), the SAME instant must be used everywhere -- set
# EVALUATION_START_TIME_ISO in the environment to share one timestamp across
# all of them; otherwise this falls back to computing its own ONCE per
# script invocation (a legitimate wall-clock read at the orchestration
# boundary, not inside the simulator's deterministic core), never a
# hardcoded literal that would go stale the next time this runs standalone.
_env_start_time = os.environ.get("EVALUATION_START_TIME_ISO")
EVALUATION_START_TIME = (
    datetime.fromisoformat(_env_start_time)
    if _env_start_time
    else datetime.now(UTC) - timedelta(hours=1)
)


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


COMPOSE = "docker compose -f docker-compose.yml -f docker-compose.override.baseline.yml"

# gaps.md sec:C.5 -- forensic root-cause finding: the fair baseline
# (services/pipeline/baseline.py::compute_and_persist_fair_baseline_run)
# evaluates up to max_retries attempts in one instantaneous batch loop,
# while RecoveryOS's real second attempt is gated by a genuine
# retry_cooldown_hours wait (workers/execution_worker.py schedules it via
# services.recovery_engine.scheduling.schedule_reevaluation_sync, fired
# later by workers/retry_scheduler.py's real 5s poll loop). At the
# production default (12h), a ~10-12 minute drain window can NEVER observe
# RecoveryOS's real attempt 2 -- confirmed as 50.3% of the canonical run's
# missed-revenue gap. PLATFORM_DEFAULT_POLICY_CONFIG_ID is the id
# services/recovery_engine/orchestrator.py::_get_or_create_default_policy_config
# lazily creates on first use; pre-seeding it here with
# retry_cooldown_hours=0 makes a rescheduled re-evaluation due immediately
# instead of 12 real hours later -- the SAME CooldownRule, the SAME
# schedule_reevaluation_sync, the SAME retry_scheduler.py, only the
# CONFIGURATION VALUE differs, and only inside this run's own freshly-wiped
# evaluation database. Production's Settings.default_retry_cooldown_hours
# (recoveryos/config.py) and every migration's server_default=12 are
# completely untouched.
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


def regenerate_dataset(seed: int):
    # Baseline override pins the diagnoser to a guaranteed-invalid key
    # (see docker-compose.override.baseline.yml's docstring) -- LLM
    # availability must not be a confound in a seed-variance study.
    run(f"{COMPOSE} down -v")
    run(f"{COMPOSE} up -d postgres redis")
    wait_for_postgres_healthy()
    time.sleep(5)
    run("python -m dotenv run -- alembic upgrade head")
    accelerate_evaluation_cooldown()
    run(
        f"python -m dotenv run -- python -m simulator.run "
        f'--n=10000 --seed={seed} --customers=2000 --scenario-weights="{{}}" '
        f"--start-time={EVALUATION_START_TIME.isoformat()} --output=db"
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


def wait_for_reevaluations_drained(timeout=600, stable_polls_required=3):
    """Round-1 draining (ledger row written) and reevaluation scheduling are
    two separate writes in workers/execution_worker.py -- pending==0 can be
    true simply because nothing has been scheduled YET, not because
    everything already fired. Requires `total` scheduled_reevaluations rows
    to stop changing across `stable_polls_required` consecutive polls (not
    just pending==0 once) before declaring the accelerated-cooldown
    second-round attempts genuinely settled."""
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
        print(
            f"  round-2 (rescheduled) drain progress: pending={pending}/{total} scheduled",
            flush=True,
        )
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


async def compute_fair_baseline_for_all_payments() -> int:
    """
    gaps.md sec:C.5 -- runs the REAL, unmodified
    services.pipeline.baseline.compute_and_persist_fair_baseline_run() for
    every failed-or-recovered payment. Must run AFTER the accelerated-
    cooldown second round has fully settled (wait_for_reevaluations_drained)
    so recovery_ledger.actual_recovery_paise reflects RecoveryOS's real
    final outcome before the two get compared. Idempotent (the function
    itself checks for an existing row per payment/experiment_id), so a
    resumed/retried seed never double-computes.

    Uses its OWN local engine, not recoveryos.database.get_app_session_factory()'s
    module-level cached singleton -- this function is called once per seed via a
    fresh asyncio.run() (collect_metrics() -> main()'s per-seed loop, all within
    ONE long-lived Python process across all 5 seeds, only the Docker containers
    get recreated). A cached engine's asyncpg connections are tied to whichever
    event loop first created them; asyncio.run() closes that loop when it
    returns, so seed 2's fresh loop reusing seed 1's cached engine breaks with
    an opaque asyncpg/proactor error -- confirmed live (seed 1 succeeded, seed 2
    failed with exactly this). Building and disposing a local engine within this
    same asyncio.run() call sidesteps the cross-loop reuse entirely.
    """
    import sys as _sys

    _sys.path.insert(0, str(REPO_ROOT))
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from recoveryos.config import get_settings
    from recoveryos.database import _build_async_engine
    from services.pipeline.baseline import compute_and_persist_fair_baseline_run

    engine = _build_async_engine(get_settings().database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            rows = (
                await session.execute(
                    _text(
                        "SELECT DISTINCT p.payment_id FROM payments p "
                        "JOIN simulator_latent_state s ON s.payment_id = p.payment_id "
                        "WHERE p.status IN ('failed', 'recovered')"
                    )
                )
            ).all()
        payment_ids = [r[0] for r in rows]

        n_done = 0
        for pid in payment_ids:
            async with session_factory() as session:
                result = await compute_and_persist_fair_baseline_run(session, pid)
                if result is not None:
                    n_done += 1
        return n_done
    finally:
        await engine.dispose()


def collect_metrics(seed: int, start_wall_time: float) -> dict:
    import asyncio

    n_fair = asyncio.run(compute_fair_baseline_for_all_payments())
    print(f"  fair baseline computed for {n_fair} payments", flush=True)

    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()

    def scalar(q, params=None):
        cur.execute(q, params or ())
        row = cur.fetchone()
        val = row[0] if row else None
        return int(val) if val is not None else None

    failed_payments = scalar("SELECT count(*) FROM payments WHERE status IN ('failed','recovered')")
    recoveryos_total = scalar("SELECT COALESCE(SUM(actual_recovery_paise),0) FROM recovery_ledger")
    # gaps.md sec:C.5 -- baseline_runs holds TWO experiments once the fair
    # baseline is computed above: PIPELINE_BASELINE_EXPERIMENT_ID (single
    # naive attempt, auto-populated per-payment by the live pipeline itself
    # via services/pipeline/ledger.py) and PIPELINE_BASELINE_FAIR_EXPERIMENT_ID
    # (same attempt budget as RecoveryOS, computed above). Filtering by
    # experiment_id here -- the old unfiltered SUM(recovered_amount_paise)
    # silently mixed both in aggregate, though only the single-attempt one
    # existed before this fix, since this harness never called the fair
    # baseline function until now.
    single_baseline_total = scalar(
        "SELECT COALESCE(SUM(recovered_amount_paise),0) FROM baseline_runs "
        "WHERE experiment_id = '00000000-0000-0000-0000-0000000000e8'"
    )
    fair_baseline_total = scalar(
        "SELECT COALESCE(SUM(recovered_amount_paise),0) FROM baseline_runs "
        "WHERE experiment_id = '00000000-0000-0000-0000-0000000000e9'"
    )
    recovered_payments = scalar(
        "SELECT count(*) FROM recovery_ledger WHERE actual_recovery_paise > 0"
    )
    revenue_at_risk = scalar("SELECT COALESCE(SUM(revenue_at_risk_paise),0) FROM recovery_ledger")
    interventions = scalar("SELECT count(*) FROM policy_decisions WHERE verdict='ALLOW'")
    blocks = scalar("SELECT count(*) FROM policy_decisions WHERE verdict='BLOCK'")
    escalates = scalar("SELECT count(*) FROM policy_decisions WHERE verdict='ESCALATE'")
    # gaps.md sec:C.5 -- RENAMED from unnecessary_intervention_rate. Traced
    # exactly: recovery_ledger.incremental_recovery_paise is ALWAYS computed
    # against the SINGLE-ATTEMPT baseline (services/pipeline/ledger.py's
    # compute_ledger_entry, every call site), never the fair one. This
    # counts ALLOW decisions where RecoveryOS's actual outcome did not
    # STRICTLY BEAT a naive single retry -- including the ~40% of cases
    # where both RecoveryOS and the naive baseline succeeded identically
    # (a genuinely necessary, successful retry, mislabeled "unnecessary"
    # under the old name). See the forensic report (gaps.md sec:C.5) for
    # the full breakdown: 0% of ALLOW decisions are ever strictly WORSE
    # than the single-attempt baseline. The old field name is not reused
    # for a differently-defined value -- this is the same query, honestly
    # renamed.
    did_not_beat_single_attempt_baseline = scalar(
        """
        SELECT count(*) FROM recovery_ledger rl
        JOIN policy_decisions pd ON pd.payment_id = rl.payment_id
        WHERE pd.verdict = 'ALLOW' AND rl.incremental_recovery_paise <= 0
        """
    )

    # gaps.md sec:C.6 -- the accelerated-cooldown fix means one payment can
    # now legitimately reach a SECOND policy_decisions row (a real replan),
    # so `interventions / failed_payments` (the old intervention_rate) can
    # exceed 100% -- it was never wrong, just mislabeled once multiple
    # rounds became reachable. Metric-correction additions below; the old
    # field/value stays for backward compatibility, relabeled in its own
    # comment, not silently redefined.
    unique_intervened_payments = scalar(
        "SELECT count(DISTINCT payment_id) FROM policy_decisions WHERE verdict = 'ALLOW'"
    )
    total_policy_decision_rounds = scalar("SELECT count(*) FROM policy_decisions")

    cur.execute(
        """
        SELECT round_num, verdict, count(*) FROM (
            SELECT verdict, ROW_NUMBER() OVER (PARTITION BY payment_id ORDER BY created_at) AS round_num
            FROM policy_decisions
        ) ranked
        GROUP BY round_num, verdict
        ORDER BY round_num, verdict
        """
    )
    round_verdict_rows = cur.fetchall()
    round_1_decisions: dict[str, int] = {}
    round_2plus_decisions: dict[str, int] = {}
    for round_num, verdict, n in round_verdict_rows:
        target = round_1_decisions if round_num == 1 else round_2plus_decisions
        target[verdict] = target.get(verdict, 0) + n

    # ── Per-seed verification checklist (gaps.md sec:C.6) ──────────────────
    scheduled_reevaluations_count = scalar("SELECT count(*) FROM scheduled_reevaluations")
    recoveries_total = scalar("SELECT count(*) FROM recoveries")
    recoveries_distinct_attempts = scalar(
        "SELECT count(DISTINCT (payment_id, attempt_number)) FROM recoveries"
    )
    cur.execute(
        "SELECT attempt_number, count(*) FROM recoveries GROUP BY attempt_number ORDER BY attempt_number"
    )
    attempts_by_number = {str(row[0]): row[1] for row in cur.fetchall()}
    ledger_total = scalar("SELECT count(*) FROM recovery_ledger")
    ledger_distinct_payments = scalar("SELECT count(DISTINCT payment_id) FROM recovery_ledger")
    expired_blocks = scalar(
        # %% escapes the literal % for psycopg2's paramstyle processing,
        # which still runs even with an empty params tuple.
        "SELECT count(*) FROM policy_decisions WHERE rule_trace::text LIKE '%%payment has expired%%'"
    )
    unsafe_ai_deltas = scalar(
        """
        SELECT count(*) FROM decision_fusion_trace dft
        JOIN policy_decisions pd ON pd.decision_id = dft.decision_id
        WHERE dft.tie_break_applied = true AND pd.verdict != 'ALLOW'
        """
    )
    retry_later_count = scalar(
        """
        SELECT count(*) FROM policy_decisions pd
        JOIN candidate_actions ca ON ca.candidate_id = pd.candidate_id
        WHERE pd.verdict = 'ALLOW' AND ca.action_type = 'RETRY_LATER'
        """
    )
    cur.execute(
        """
        SELECT rule_trace FROM policy_decisions WHERE verdict = 'BLOCK'
        """
    )
    block_reason_counts: dict[str, int] = {}
    for (trace,) in cur.fetchall():
        failing = [s for s in trace if not s.get("passed", True)]
        if failing:
            rule = failing[0]["rule"]
            block_reason_counts[rule] = block_reason_counts.get(rule, 0) + 1

    # Confusion matrix + cooldown-artifact-bucket check, against the FAIR
    # baseline computed above -- must be run AFTER the fair baseline exists.
    cur.execute(
        """
        WITH reos AS (
            SELECT payment_id, (actual_recovery_paise > 0) AS recovered
            FROM recovery_ledger
        ),
        fair AS (
            SELECT payment_id, (outcome = 'RECOVERED') AS recovered
            FROM baseline_runs WHERE experiment_id = '00000000-0000-0000-0000-0000000000e9'
        )
        SELECT fair.recovered, reos.recovered, count(*)
        FROM fair JOIN reos ON reos.payment_id = fair.payment_id
        GROUP BY fair.recovered, reos.recovered
        """
    )
    confusion = {
        f"baseline_{'recovered' if br else 'missed'}_recoveryos_{'recovered' if rr else 'missed'}": n
        for br, rr, n in cur.fetchall()
    }
    cur.execute(
        """
        WITH missed AS (
            SELECT rl.payment_id
            FROM recovery_ledger rl
            JOIN baseline_runs br ON br.payment_id = rl.payment_id AND br.experiment_id = '00000000-0000-0000-0000-0000000000e9'
            WHERE br.outcome = 'RECOVERED' AND rl.actual_recovery_paise = 0
        )
        SELECT (SELECT count(*) FROM recoveries r WHERE r.payment_id = m.payment_id) AS real_attempts, count(*)
        FROM missed m GROUP BY real_attempts
        """
    )
    missed_by_real_attempts = {str(row[0]): row[1] for row in cur.fetchall()}
    # The cooldown-timing artifact (gaps.md sec:C.5) showed up as missed
    # payments with EXACTLY 1 real attempt (RecoveryOS's attempt-1 failed,
    # never reached a real attempt-2 the fair baseline's counterfactual
    # credited itself with). Must be 0 now.
    cooldown_artifact_bucket = missed_by_real_attempts.get("1", 0)

    diagnoses_total = scalar("SELECT count(*) FROM diagnoses")
    abstentions = scalar("SELECT count(*) FROM diagnoses WHERE root_cause = 'unknown'")

    TRUE_TO_EXPECTED_ROOT_CAUSE = {  # noqa: N806 -- constant-style mapping, not a variable
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
    committed = [(tft, rc) for tft, rc in root_cause_rows if rc != "unknown"]
    correct_committed = sum(
        1 for tft, rc in committed if TRUE_TO_EXPECTED_ROOT_CAUSE.get(tft) == rc
    )

    elapsed_seconds = time.time() - start_wall_time
    avg_latency_ms_per_payment = (
        (elapsed_seconds / failed_payments) * 1000 if failed_payments else None
    )

    conn.close()

    return {
        "seed": seed,
        "model": "logistic_regression (model_lr.pkl, gaps.md SC.2 -- LR beat LightGBM on clean temporal holdout)",
        "sim_start_time": EVALUATION_START_TIME.isoformat(),
        "evaluation_cooldown_accelerated": True,  # gaps.md sec:C.5 -- see accelerate_evaluation_cooldown()
        "failed_payments": failed_payments,
        "recoveryos_total_paise": recoveryos_total,
        # kept for continuity with historical artifacts -- this is the
        # SINGLE-ATTEMPT baseline, same as before this fix (see comment above).
        "baseline_total_paise": single_baseline_total,
        "incremental_recovery_paise": recoveryos_total - single_baseline_total,
        # NEW -- gaps.md sec:C.5 -- the genuinely fair, same-attempt-budget,
        # now-temporally-aligned comparison. Prefer this field over
        # incremental_recovery_paise when characterizing RecoveryOS's real
        # decision quality.
        "fair_baseline_total_paise": fair_baseline_total,
        "incremental_recovery_vs_fair_baseline_paise": recoveryos_total - fair_baseline_total,
        "revenue_at_risk_paise": revenue_at_risk,
        "recovery_rate": recovered_payments / failed_payments if failed_payments else None,
        "revenue_recovery_rate": recoveryos_total / revenue_at_risk if revenue_at_risk else None,
        # gaps.md sec:C.6 -- KEPT for backward compatibility (same field
        # name/value as before: ALLOW-decision-rows / failed_payments).
        # RELABEL, not redefine: with the accelerated-cooldown fix, one
        # payment can legitimately produce 2 ALLOW rows (round 1 + a real
        # replan), so this can exceed 1.0 -- it is an ALLOW decision-ROUND
        # rate, not "the fraction of payments RecoveryOS intervened on".
        # Use unique_intervention_rate for that question.
        "intervention_rate": interventions / failed_payments if failed_payments else None,
        # NEW, correct replacements --
        "unique_intervened_payments": unique_intervened_payments,
        "unique_intervention_rate": (
            unique_intervened_payments / failed_payments if failed_payments else None
        ),
        "total_policy_decision_rounds": total_policy_decision_rounds,
        "decision_round_rate": (
            total_policy_decision_rounds / failed_payments if failed_payments else None
        ),
        "round_1_decision_distribution": round_1_decisions,
        "round_2plus_decision_distribution": round_2plus_decisions,
        # RENAMED from unnecessary_intervention_rate -- see the comment at
        # its computation above for the exact semantic correction.
        "did_not_beat_single_attempt_baseline_rate": (
            did_not_beat_single_attempt_baseline / interventions if interventions else None
        ),
        "policy_blocks": blocks,
        "policy_escalates": escalates,
        "policy_block_cause_decomposition": block_reason_counts,
        "root_cause_accuracy_raw": correct_committed / diagnoses_total if diagnoses_total else None,
        "root_cause_accuracy_committed_only": (
            correct_committed / len(committed) if committed else None
        ),
        "abstention_rate": abstentions / diagnoses_total if diagnoses_total else None,
        "wall_clock_seconds_full_run": elapsed_seconds,
        "avg_throughput_latency_ms_per_payment": avg_latency_ms_per_payment,
        # ── Per-seed verification checklist (gaps.md sec:C.6) ──────────────
        "verification": {
            "scheduled_reevaluations_count": scheduled_reevaluations_count,
            "recoveries_total": recoveries_total,
            "recoveries_distinct_attempts": recoveries_distinct_attempts,
            "recoveries_duplicate_attempts": recoveries_total - recoveries_distinct_attempts,
            "attempts_by_number": attempts_by_number,
            "ledger_total": ledger_total,
            "ledger_distinct_payments": ledger_distinct_payments,
            "ledger_duplicate_payments": ledger_total - ledger_distinct_payments,
            "expiry_pathology_count": expired_blocks,
            "unsafe_ai_deltas": unsafe_ai_deltas,
            "retry_later_count": retry_later_count,
            "confusion_matrix": confusion,
            "missed_by_real_attempts": missed_by_real_attempts,
            "cooldown_artifact_bucket": cooldown_artifact_bucket,
        },
    }


def main():
    print(
        f"sim_start_time (shared across all seeds this run): {EVALUATION_START_TIME.isoformat()}",
        flush=True,
    )
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
        wait_for_reevaluations_drained()
        metrics = collect_metrics(seed, start)
        print(json.dumps(metrics, indent=2), flush=True)
        results.append(metrics)
        RESULTS_FILE.write_text(json.dumps(results, indent=2))
        print(f"seed={seed} done, results saved to {RESULTS_FILE}", flush=True)

    print("\nALL SEEDS COMPLETE")


if __name__ == "__main__":
    main()
