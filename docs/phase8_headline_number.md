# Phase 8 — Headline Incremental-Revenue Number

TRD §7's core deliverable: computed once, honestly, before any tuning. Recorded here
immediately after computation — nothing below has been adjusted after the fact.

## Provenance

Run against the canonical dataset described in [phase8_canonical_run.md](phase8_canonical_run.md)
(`simulation_id=b5345e16-0670-5c0f-bc83-c449e1f4a576`, 10,000 payments / 950 failed, seed=42),
processed through the full live pipeline (`services/pipeline/consumer.py` →
`services/diagnosis_engine` → `services/policy_engine` → `services/execution_engine` →
`recovery_ledger`/`baseline_runs`) on code that includes every fix from this audit sweep
(S1-S4, R1-R2, I1-I5, M1-M4, MD1-MD4, W1, SIM1). Real wall-clock run: `2026-08-26T07:07Z`-ish
through full drain, confirmed via `stream:risk_engine` consumer-group lag/pending both reaching 0.

## Full-run integrity (acceptance criteria for the run itself)

| Check | Result |
|---|---|
| Run completes, unhandled exceptions | **0** — no exceptions/tracebacks in `event_processor` or `pipeline_orchestrator` logs across the full run |
| Real error rate (honest, non-zero) | LLM diagnoser calls: 950/950 payments hit `AuthenticationError` (401 — no real `OPENAI_API_KEY` configured in this dev environment, only `sk-placeholder`), 950/950 correctly fell back to the deterministic diagnoser (`diagnoses.is_fallback = true` for all 950, confirmed via direct query — not the log line count, which undercounted by 4 due to a log-buffering artifact; the DB is authoritative) |
| `recovery_ledger` row count | **950** = failed payment count exactly. Distinct `payment_id` count also **950** — zero duplicates |
| `audit_log` row count / orphans | **950** rows, **0 orphans** (verified via LEFT JOIN against payments/diagnoses/candidate_actions/policy_decisions — every non-null FK resolves) |
| `candidate_actions` count | **5,700** = 950 × 6 exactly |
| `policy_decisions` verdicts | 913 ALLOW / 37 BLOCK — `recoveries` count (913) matches ALLOW count exactly |
| `baseline_runs` | 950, all under the single `PIPELINE_BASELINE_EXPERIMENT_ID` sentinel; outcomes 379 RECOVERED / 349 NOT_RECOVERED / 222 NOT_ATTEMPTED |

## Scale-proofs re-confirmed against this exact run (not just smaller reproduction tests)

- **S1 (ledger/diagnosis dedup under redelivery, at scale):** after the run fully drained, 5
  already-fully-processed payments were each redelivered twice more (10 forced redeliveries
  total) directly to `stream:risk_engine` with their real, original `source_event_id`. Every
  per-payment count (diagnoses/candidate_actions/policy_decisions/recovery_ledger/recoveries/
  audit_log) and every global table total was byte-identical before and after — zero new rows
  anywhere. Confirms S1 holds against the fully-populated 950-row tables, not just a
  fresh/empty-table unit test.
- **S3 (stale anomaly-window suppression):** the real run's own anomaly detection never
  produced a HIGH-severity window (all 12 windows generated were `insufficient_data` severity,
  spanning only 15 real minutes — not long enough to test staleness organically). Directly
  proved live instead: synthetic HIGH-severity `anomaly_windows` rows inserted for two fake
  bank cohorts (`TESTBANK_STALE` at 45 minutes old, `TESTBANK_FRESH` at 5 minutes old, both
  cleaned up afterward), `is_cohort_suppressed()` called directly against the live app_role DB
  connection: stale → `None` (correctly not suppressing), fresh → active `SuppressionInfo`
  (correctly still suppressing). Matches `tests/integration/test_anomaly_suppression_freshness.py`
  exactly, now demonstrated against the live stack.
- **S2 (LLM adversarial guards):** `tests/unit/test_diagnosis_adversarial.py` and
  `tests/unit/test_llm_diagnoser_guards.py` (9 tests total, including the two S3 integration
  tests run together) re-confirmed passing against current code. **Caveat, stated honestly:**
  this run had no real `OPENAI_API_KEY`, so the LLM path never returned a real (adversarial or
  otherwise) response in production — S2's guard against an overconfident/adversarial LLM
  response is proven by unit test, not by this live run. Only the "LLM unreachable → fallback"
  path (a different, pre-existing mechanism) was exercised live, 950/950 times.

  **CORRECTED framing (Domain Audit finding A5):** the caveat above is honest as far as it
  goes, but it implicitly frames the gap as "the LLM didn't get to run for this evaluation."
  The more consequential fact, established separately by the Domain Audit's finding F1: **the
  headline `incremental_recovery` number below would be byte-identical even if the LLM had run
  950/950 times successfully.** `services/recovery_engine/orchestrator.py:build_decision()`
  never reads `Diagnosis.root_cause`/`confidence`/`evidence` — `chosen_action`, EVI, and the
  policy verdict for every one of these 950 payments come entirely from the certified
  propensity model + EVI + anomaly context + the 10 static policy rules, none of which touch
  the diagnosis row. So the 0%-real-LLM-calls rate in this run is not a caveat on the
  *validity* of the number below — it is simply not a variable the number depends on at all.

## The number — computed via the exact query specified

```sql
WITH recoveryos_result AS (
    SELECT payment_id, actual_recovery_paise FROM recovery_ledger
),
baseline_result AS (
    SELECT payment_id, recovered_amount_paise AS baseline_recovery_paise FROM baseline_runs
)
SELECT
    SUM(r.actual_recovery_paise) AS recoveryos_total,
    SUM(b.baseline_recovery_paise) AS baseline_total,
    SUM(r.actual_recovery_paise) - SUM(b.baseline_recovery_paise) AS incremental_recovery
FROM recoveryos_result r
JOIN baseline_result b USING (payment_id);
```

Joined row count: **950** (every `recovery_ledger` row has a matching `baseline_runs` row —
no partial coverage).

| Metric | Value (paise) | Value (₹) |
|---|---:|---:|
| `recoveryos_total` | 97,763,204 | ₹977,632.04 |
| `baseline_total` | 93,514,016 | ₹935,140.16 |
| **`incremental_recovery`** | **4,249,188** | **₹42,491.88** |

Incremental recovery is **+4.5% over the naive baseline** (4,249,188 / 93,514,016).

## Independent cross-check (TRD §7's "third party could reproduce it" proof)

Raw export of `recovery_ledger` (950 rows) and `baseline_runs` (950 rows) via `pandas.read_sql`,
merged independently in Python on `payment_id` (inner join, 950 rows — matches the SQL join
count exactly), summed with plain `int()` arithmetic (both columns are `int64`/`BIGINT` paise —
no floats anywhere in this computation):

| Metric | SQL | Python (independent) | Match |
|---|---:|---:|---|
| `recoveryos_total` | 97,763,204 | 97,763,204 | exact |
| `baseline_total` | 93,514,016 | 93,514,016 | exact |
| `incremental_recovery` | 4,249,188 | 4,249,188 | exact |

Zero tolerance, exact integer match on all three values.

## Domain Audit finding #6 — attempt-budget fairness (added 2026-08-28, NOT yet re-run at this canonical scale)

The number above compares RecoveryOS's real recovered revenue against `services/pipeline/
baseline.py`'s naive strategy modeling exactly ONE retry attempt — while RecoveryOS's own
path can execute up to `policy_configs.max_retries` real attempts (`CooldownRule`/
`RetryLimitRule`/`scheduled_reevaluations`). The Domain Audit's question, stated plainly:
*how much of `incremental_recovery` above reflects "we tried more times" rather than "we
chose better actions"?*

**What was built to answer this**: `compute_and_persist_fair_baseline_run()` (services/
pipeline/baseline.py) — the SAME naive decision policy (retry everything except a
known-hopeless failure, no EVI/propensity/timing intelligence), given the SAME attempt
budget RecoveryOS itself is allowed for that payment's merchant. Each simulated attempt
re-derives `true_recovery_prob_bps` via the exact same attempt-decay function
(`_recompute_attempt_aware_prob_bps`) `SimulatorAdapter.retry()` uses for RecoveryOS's own
real executed attempts, and resolves via the same shared `resolve_simulated_outcome()`.
Decomposes cleanly, by construction:

```
scoped_incremental_recovery_paise == attributable_to_more_attempts_paise
                                    + attributable_to_better_decisions_paise
```

Exposed live at `GET /v1/experiments/live`'s `fair_comparison` block. Verified for real
against the live docker-compose stack (a single synthetic payment: RecoveryOS recovered
₹3,000, the fair 2-attempt baseline recovered ₹0 — a real, unlucky dice roll at 15%
probability across 2 tries, not a favorable cherry-pick — giving
`attributable_to_more_attempts=0, attributable_to_better_decisions=₹3,000`), and by 7 unit/
integration tests (`tests/unit/test_ledger_correction_invariant.py`,
`tests/integration/test_fair_baseline.py`, `tests/integration/test_experiments_fair_comparison.py`)
proving the mechanism monotonic (more attempts never recovers less), budget-respecting
(never exceeds `max_retries`), and correctly scoped (an earlier version of the live
`/v1/experiments/live` endpoint mixed a full 341-payment old-baseline sum against a
1-payment fair-baseline sum, producing a nonsensical negative "attributable to more
attempts" — caught live-testing, fixed to scope every figure in `fair_comparison` to the
exact same payment set).

**What has NOT been done**: the +₹42,491.88 headline number above (and the 5-seed
±₹70,257.61 mean in `docs/phase8_priority0_multi_seed_baseline.md`) has **not** been
recomputed with the fair baseline at the full 950-payment/10,000-payment canonical scale.
Doing so honestly requires re-running the full canonical dataset through both
`compute_and_persist_baseline_run` (existing, unchanged) and `compute_and_persist_fair_
baseline_run` (new) and re-deriving the SQL/pandas cross-check above with the
`fair_comparison` decomposition — a real, sizeable evaluation run this document does not
claim to have performed. The capability is built, tested, and live-verified at small scale;
the full-scale re-run is a genuine next step, not yet done.

## Adversarial Audit Verdict finding — dice-roll reproducibility (fixed 2026-08-29)

`integrations.razorpay.adapter.resolve_simulated_outcome()` — the single dice-roll every
number on this page ultimately depends on (both `SimulatorAdapter.retry()`'s real executed
attempts and `services/pipeline/baseline.py`'s counterfactual replay call it) — used to draw
from Python's process-seeded global `random` module, and its sibling
`_recompute_attempt_aware_prob_bps()` used a fresh `uuid.uuid4()`-seeded `SimRng` per call.
Neither was reproducible: re-running the identical dataset seed through the pipeline twice
could produce two different headline numbers, silently breaking the "seed=N is a
reproducible evaluation" claim every multi-seed figure on this page and in
`docs/phase8_evaluation_table.md` relies on.

Fixed by hashing a stable `(payment_id, attempt_number, purpose)` identity into the draw
(`_deterministic_bps_draw`, SHA-256-based — not Python's builtin `hash()`, which is itself
per-process randomized via `PYTHONHASHSEED`). Same identity now always yields the same
draw, in this process or any other. Regression coverage:
`tests/integration/test_baseline_determinism_and_dedup.py`. This does NOT change the
figures already recorded above — this fix makes re-deriving them, and any future canonical
re-run, actually reproducible; it was not run again to "improve" the number.

## Standing instruction

**This is the first, honest reading of Phase 8's headline number, recorded before any
model/policy tuning.** Any subsequent iteration on model parameters, policy thresholds, or
calibration must be evaluated against this baseline reading, not by re-deriving a fresh "first"
number after changes have already been made.
