# RecoveryOS — Phase 8 Evaluation Report

> **Superseded — not the current headline number.** This report's own +₹42,491.88 (+4.5%)
> figure below is from a single seed against the single-attempt baseline. The CURRENT headline
> number is +₹73,181.78 mean incremental recovery (positive in all 5 seeds), computed against a
> compliance-aware baseline (the real policy rule chain, not a weaker strawman) — see README §9
> and [`tests/evaluation/artifacts/multi_seed_compliance_aware_aggregate.json`](artifacts/multi_seed_compliance_aware_aggregate.json).
> Re-Audit finding: a reader who finds this file directly (not via README's own links, which
> don't reference it) would otherwise cite the older, weaker number. Kept for its detailed
> methodology/reproduction record below, not as the current result.

This is the single, self-contained record of Phase 8 (TRD §7's evaluation harness): the
headline incremental-revenue number, how it was produced, every secondary metric, the AI
evaluation, the adversarial test results, and — most importantly — exactly which of this
codebase's fixes this number actually depends on. A third party should be able to reproduce
everything below from this file plus the repo alone, without trusting any dashboard.

**Raw artifact:** [`tests/evaluation/artifacts/phase8_payment_outcomes.csv`](artifacts/phase8_payment_outcomes.csv)
(also available as [`.parquet`](artifacts/phase8_payment_outcomes.parquet)) — one row per
payment in the canonical dataset (10,000 rows, 33 columns), joining `payments`,
`simulator_latent_state` (ground truth), `diagnoses`, `policy_decisions`, `candidate_actions`,
`recoveries`, `recovery_ledger`, and `baseline_runs`. The headline number and every metric in
this report are independently re-derivable from this one file — see "Reproduce it yourself"
at the bottom.

---

## 1. Provenance — exact seed, scenario mix, timestamp

| Field | Value |
|---|---|
| Command | `python -m simulator.run --n=10000 --seed=42 --customers=2000 --scenario-weights="{}" --output=db` |
| Seed | `42` |
| Scenario weights | `{}` — every scenario's own built-in default (a coincidental match to `calibration/parameters.yaml`, not read from it — see `gaps.md` §C.3) |
| `simulation_id` | `b5345e16-0670-5c0f-bc83-c449e1f4a576` |
| Generator version | `simulator-v2.0` |
| Real wall-clock generation | Originally `2026-08-26T06:42:07Z`; regenerated once after an unrelated cleanup-script bug (see §6), physically re-run at `2026-08-26T06:54Z` — byte-identical content, confirmed by re-running `test_deterministic_seed_reproducibility` and `test_ground_truth_not_derivable_from_visible_features` against the new copy |
| Migrations | `0001`–`0014` (head), applied to a genuinely empty database (`docker compose down -v` before each generation) |
| Code commit | `9353ec3` at generation time; this report and its supporting docs are additional commits on top |
| Volume | 10,000 payments (9,050 success / 950 failed), 2,000 customers, 3 merchants, 20,000 canonical events, 10,000 latent-state records, 6 banks, 4 payment methods |
| Non-circularity | `max_auc=0.7264` (ceiling 0.85, floor 0.52) — `test_ground_truth_not_derivable_from_visible_features` |
| Reproducibility | Byte-identical re-run, `diff_count=0` — `test_deterministic_seed_reproducibility` |

Full detail: [`docs/phase8_canonical_run.md`](../../docs/phase8_canonical_run.md).

Full live-pipeline processing (all 950 failed payments published to `stream:payment_failed`,
drained through `event_processor` → `pipeline_orchestrator` → `recovery_ledger`/`baseline_runs`)
completed with **0 unhandled exceptions**, `recovery_ledger` = 950 (exact, zero duplicates),
`candidate_actions` = 5,700 = 950×6 exactly, `audit_log` = 950 with 0 orphans. Full detail:
[`docs/phase8_headline_number.md`](../../docs/phase8_headline_number.md).

---

## 2. The headline number

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

| Metric | Paise | ₹ |
|---|---:|---:|
| `recoveryos_total` | 97,763,204 | ₹977,632.04 |
| `baseline_total` | 93,514,016 | ₹935,140.16 |
| **`incremental_recovery`** | **4,249,188** | **₹42,491.88** (+4.5% over baseline) |

Cross-checked independently via `pandas` (raw export of both tables, separate `int64` merge/sum)
— **exact match, zero tolerance**, on all three values. Full detail:
[`docs/phase8_headline_number.md`](../../docs/phase8_headline_number.md).

---

## 3. Secondary metrics — PRD §35's table, real numbers

| Metric | Baseline | RecoveryOS |
| --- | ---: | ---: |
| Payments evaluated (total dataset) | 10,000 | 10,000 |
| Eligible failed payments | 950 | 950 |
| Revenue at risk | ₹23,59,678.31 | ₹23,59,678.31 |
| Recovered revenue | ₹9,35,140.16 | ₹9,77,632.04 |
| Recovery rate | 379/950 = 39.89% | 402/950 = 42.32% |
| Revenue recovery rate | 39.63% | 41.43% |
| Incremental recovery | — | +₹42,491.88 (+4.5%) |
| Intervention rate | 728/950 = 76.63% | 913/950 = **96.11%** |
| Unnecessary intervention rate | N/A (no comparator) | 801/913 = **87.74%** |
| Avg. recovery value per intervention | ₹1,284.53 | ₹1,070.79 |
| Stopping rule compliance rate | N/A (0 workflows required stopping this run) | N/A |

Two results are honestly surprising, not smoothed over: RecoveryOS *intervenes more* than the
naive baseline (96.1% vs 76.6% — the opposite direction from PRD's illustrative table), and the
87.7% "unnecessary intervention" figure is dominated by irreducible stochastic-draw agreement
between two independent outcome resolutions (both succeed / both fail), not policy waste — full
per-payment outcome breakdown in [`docs/phase8_evaluation_table.md`](../../docs/phase8_evaluation_table.md).

---

## 4. AI evaluation — PRD §36, against ground truth

Computed via `app_role` (`DATABASE_URL`, what `get_app_session_factory()` uses) — the only role
permitted to see `simulator_latent_state`. Confirmed live that the restricted `inference` login
(`inference_role`) gets `permission denied for table simulator_latent_state` on the identical
query.

**Root-cause accuracy: 213/950 = 22.42% raw** — but all 950 diagnoses ran through the
deterministic fallback (no live LLM key this run), and the raw number conflates *abstention*
with *wrongness*:

| | Count | % |
|---|---:|---:|
| Abstained (`root_cause = unknown`) | 716 | 75.4% |
| Committed to a diagnosis | 234 | 24.6% |
| — correct | 213 | **91.03%** of committed |

**Action-recommendation accuracy: 944/950 = 99.37%**, against a ground-truth-optimal action
re-derived from `services/recovery_engine/evi.py`'s own EVI formula with the true recovery
probability substituted in.

Full detail, including the exact (judgment-call, explicitly documented) mappings used:
[`docs/phase8_ai_evaluation.md`](../../docs/phase8_ai_evaluation.md).

---

## 5. Adversarial testing — PRD §37, all 5 scenarios

| # | Scenario | Result |
|---|---|---|
| 1 | Missing information | PASS |
| 2 | Conflicting information | **PASS — including direct re-confirmation that S2's guard fires on the real LLM-response path**, not just the fallback path |
| 3 | Systemic degradation | PASS |
| 4 | Previously recovered payment | **FAIL at evaluation time → FIXED (Task E1).** `CooldownRule` was purely elapsed-time-based with no awareness of a prior `SUCCESS` outcome. Fixed: `payments.status='recovered'` is now set on a real `SUCCESS`, and `EligibilityRule` (first in rule order) blocks on it unconditionally. Re-proven live + 2 dedicated regression tests. |
| 5 | Repeated failures | PASS — proven live (no prior test existed): a synthetic backdated prior attempt was injected for a real payment, `RetryLimitRule` correctly fired `ESCALATE` on the third attempt |

Scenarios 4 and 5 had no existing test coverage before this evaluation; both were proven directly
against the live containers rather than reported as untested. Full detail, including the exact
`rule_trace` evidence for scenarios 4 and 5:
[`docs/phase8_ai_evaluation.md`](../../docs/phase8_ai_evaluation.md).

---

## 6. Why this number can be trusted — the fixes this evaluation specifically depends on

This session ran a full directory-by-directory audit before Phase 8, and produced a long list of
fixes (S1–S4, R1–R2, I1–I5, M1–M4, MD1–MD4, W1, SIM1 — see `gaps.md`). Most of those are not
directly load-bearing for *this specific number*. Two are:

### S1 — `recovery_ledger`/diagnosis dedup under redelivery (migration `0013`)

Redis Streams is at-least-once delivery. Without a real dedup constraint, a redelivered
`stream:risk_engine` message for a payment already fully processed would insert a **second**
`recovery_ledger` row for the same payment — directly inflating `recoveryos_total` in §2's SQL
sum. `recovery_ledger` has `UNIQUE(payment_id)`; `diagnoses`/`candidate_actions`/
`policy_decisions` have `UNIQUE(payment_id, source_event_id)`.

**Why this specific evaluation needed it proven, not assumed:** this session didn't just trust
the migration exists — after the full 950-payment run completed, 5 already-fully-processed
payments were each redelivered twice more (10 forced redeliveries total, reusing their real
original `source_event_id`) directly against the live, fully-populated 950-row tables. Every
per-payment and global table count was byte-identical before and after — zero new rows anywhere.
If this fix were missing or broken, `recovery_ledger`'s row count would exceed 950 and
`recoveryos_total` would be silently inflated by however many redeliveries happened to occur
during the real run (Redis Streams' at-least-once semantics guarantee some will, at scale — this
is not a hypothetical). Full evidence: [`docs/phase8_headline_number.md`](../../docs/phase8_headline_number.md).

### The LR-not-LightGBM correction (`gaps.md` §C.2)

Phase 2's original certificate claimed LightGBM beat Logistic Regression by 0.0401 AUC and was
the correct production model. Re-auditing the actual train/val/test parquet files (not just the
certificate's summary) found `val_random` and `test_scenario` were each generated by a
separately-reseeded `simulator.dataset.builder` call sharing the same seed as `train` —
58.8%/58.3% of those rows are verbatim duplicates of training rows. On `test_temporal`, the one
split verified to have **zero** overlap with `train`, LR's AUC (0.8378) is marginally *higher*
than LightGBM's (0.8374, 95% CI overlaps LR entirely) — LightGBM does not clear TRD §3.3's >0.03
lift gate on real held-out data. **Fix:** `services/recovery_engine/propensity.py` loads
`model_lr.pkl` + `feature_transformer_v1.pkl`, not `model_lightgbm.txt`.

**Why this specific evaluation needed it:** `candidate_actions.recovery_prob_bps` (the input to
every EVI calculation, every policy decision, and — via `baseline.py`'s shared
`resolve_simulated_outcome()` — indirectly every executed outcome) comes from whichever
propensity model production actually loads. Had this evaluation run against the LightGBM
artifact instead — the one the *original*, uncorrected certificate said was better — every
`recovery_prob_bps` value feeding both the live decisions and the ground-truth-optimal-action
recomputation in §4 would be numerically different, changing which candidates clear the EVI
floor, which get `ALLOW`ed, and therefore the entire recovery-rate and incremental-recovery
computation. `test_lgbm_does_not_beat_baseline_on_the_real_holdout_so_lr_stays_default`
(`tests/unit/test_propensity.py`) locks this in — it fails loudly if the artifacts are ever
regenerated in a way that reverses the choice.

### Also relevant, for completeness

- **S4** (`migrations/0014`, `event_publications`): decouples "was this event newly inserted"
  from "was it actually published downstream." Without it, a payment could have a canonical
  Postgres row but never actually enter `stream:risk_engine` — meaning it would be silently
  absent from `recovery_ledger`/`baseline_runs` entirely rather than double-counted. Proven live
  via an 8-payment bridge test (delete-and-redeliver an `event_publications` row, confirm
  `event_processor` re-publishes on redelivery even though `is_new=False`).
- **MD1** (tracked model artifacts): confirms `model_lr.pkl`/`feature_transformer_v1.pkl` are
  genuinely present in a fresh clone (not silently falling back to an untrained/default model),
  which is what makes the LR-not-LightGBM fix above actually take effect at runtime rather than
  being correct in principle but unreachable.

Everything else in this session's audit (W1's Celery removal, SIM1's customer-generator
timestamp fix, the calibration-binding deferral in `gaps.md` §C.3, the PRD payment-methods drift
in §C.5, etc.) is real and worth having fixed, but does not change any number in this report —
none of it touches `recovery_prob_bps`, `recovery_ledger`, or `baseline_runs`.

---

## 7. Known limitations of this specific run (stated, not hidden)

- No live `OPENAI_API_KEY` was configured — all 950 diagnoses went through the deterministic
  fallback, not a real LLM call. Root-cause accuracy and S2's adversarial guard are proven on
  the fallback + a mocked-LLM-response unit test respectively, not on a real LLM response in
  production.
- "Stopping rule compliance" is undefined for this run (0/0) — this was a single-pass batch
  publish completed in minutes, not a live multi-day scheduler, so no payment ever reached a
  second attempt naturally. Scenario 5 was proven instead via a synthetic backdated attempt
  history (§5).
- Scenario 4 (previously recovered payment) was a **confirmed gap at evaluation time — since
  fixed as Task E1** (`payments.status='recovered'` + `EligibilityRule` guard, re-proven live,
  2 regression tests added, full suite green). See §5.

---

## Reproduce it yourself

1. From the repo alone: `docker compose down -v && docker compose up -d && alembic upgrade head`,
   then run the exact command in §1's provenance table. Confirm the volume/scenario-mix numbers
   in [`docs/phase8_canonical_run.md`](../../docs/phase8_canonical_run.md) match.
2. Publish all `PAYMENT_FAILED`-type canonical events to `stream:payment_failed` (reusing their
   real `event_id`/`idempotency_key` — see [`docs/phase8_headline_number.md`](../../docs/phase8_headline_number.md)
   for the exact approach) and let the pipeline drain.
3. Run §2's SQL directly against the resulting `recovery_ledger`/`baseline_runs`, or
4. Skip 1–3 entirely and just load [`artifacts/phase8_payment_outcomes.csv`](artifacts/phase8_payment_outcomes.csv)
   — every number in this report derives from that one file:

```python
import pandas as pd
df = pd.read_csv("tests/evaluation/artifacts/phase8_payment_outcomes.csv")
scored = df.dropna(subset=["actual_recovery_paise", "baseline_recovered_amount_paise"])
recoveryos_total = int(scored["actual_recovery_paise"].sum())
baseline_total = int(scored["baseline_recovered_amount_paise"].sum())
print(recoveryos_total, baseline_total, recoveryos_total - baseline_total)
# 97763204 93514016 4249188
```
