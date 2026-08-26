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

## Standing instruction

**This is the first, honest reading of Phase 8's headline number, recorded before any
model/policy tuning.** Any subsequent iteration on model parameters, policy thresholds, or
calibration must be evaluated against this baseline reading, not by re-deriving a fresh "first"
number after changes have already been made.
