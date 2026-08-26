# Phase 8 — Canonical 10k Dataset Run Record

This is the durable record TRD §7 requires: enough for a third party to reproduce this exact
run from a fresh checkout and confirm the same data. Written immediately after generation, before
any tuning — nothing below has been adjusted after the fact.

## Provenance

| Field | Value |
|---|---|
| Command | `python -m simulator.run --n=10000 --seed=42 --customers=2000 --scenario-weights="{}" --output=db` |
| Seed | `42` |
| Scenario weights | `{}` — every scenario's own built-in default (see gaps.md §C.3: these defaults are a **coincidental match** to `calibration/parameters.yaml`'s real-world-sourced values, not read from it; explicitly not touched for this run per the standing deferral) |
| `generator_version` | `simulator-v2.0` |
| `simulation_id` | `b5345e16-0670-5c0f-bc83-c449e1f4a576` |
| Simulated clock start (`simulator_manifests.created_at`) | `2026-08-20T09:00:00Z` (fixed `SimClock` default, not wall-clock) |
| Real wall-clock generation timestamp | `2026-08-26T06:42:07Z` |
| DB state before generation | `docker compose down -v` (full volume wipe) → `alembic upgrade head` on a genuinely empty database → this is the only data in it |
| Migrations applied | `0001` through `0014` (head = `0014`), confirmed via `alembic_version` table, not assumed |
| Code commit | `9353ec3` (`fix: apps/api/Dockerfile missing pandas/scikit-learn/pyyaml` — the last commit before this run; `git status` was clean at generation time) |

## Pre-generation verification (all re-confirmed against this exact commit before running)

- S1 (`recovery_ledger` dedup): `uq_recovery_ledger_payment` constraint confirmed present via direct `pg_constraint` query on this fresh schema; `test_pipeline_redelivery_dedup.py`'s two tests re-run and pass.
- MD1 (tracked model artifacts): genuinely fresh clone, `model_lr.pkl`/`feature_transformer_v1.pkl` present, `model_lightgbm.txt` absent, real prediction produced.
- W1 (dead Celery removed): stale orphan `recoveryos_worker` container found and removed; `docker compose ps -a` confirmed zero worker/celery container anywhere.
- Stray `localhost:5432` process: confirmed a real, separate native Postgres process (PID 7056), unrelated to this project. Confirmed unreachable — `.env` points every DSN at `localhost:5433` (this project's own container), and the config defaults' `CHANGE_ME` fallback was directly tested against port 5432 and correctly fails authentication rather than silently connecting.
- gaps.md §C.3 (calibration deferral note): confirmed present, unmodified.

## Generated volume

| Entity | Count |
|---|---|
| Payments | 10,000 |
| — success | 9,050 (90.5%) |
| — failed | 950 (9.5%) |
| Customers | 2,000 |
| Merchants | 3 |
| Events | 20,000 |
| Latent state records | 10,000 |
| Distinct banks | 6 |
| Distinct payment methods | 4 (PRD §31 says "5 payment methods" — a pre-existing PRD/code mismatch from Phase 1, not something introduced or fixed by this run; `PAYMENT_METHODS` has always been `["upi", "card", "netbanking", "wallet"]`) |

## Scenario mix — verified against the actual persisted data, not assumed from config

Queried directly from `simulator_latent_state.true_failure_type` (the ground-truth label, all 10,000 rows) and `payments.failure_class`/`failure_code` (the observed/noisy telemetry, the 950 failed rows) — not a re-generated in-memory sample.

**True failure type (ground truth), all 10,000 payments:**

| `true_failure_type` | Count | Maps to PRD Scenario |
|---|---|---|
| SUCCESS | 9,050 | (not a failure) |
| TEMPORARY_GATEWAY_TIMEOUT | 316 | D — Temporary timeout |
| CUSTOMER_INSUFFICIENT_FUNDS | 208 | F — Customer-specific failure |
| PERMANENT_INVALID_CREDS | 154 | E — Permanent failure |
| TRANSIENT_NETWORK_DROP | 126 | A — Normal failure |
| PERMANENT_EXPIRED_INSTRUMENT | 56 | E — Permanent failure |
| PERMANENT_ACCOUNT_CLOSED | 51 | E — Permanent failure |
| BANK_DEGRADATION_FAIL | 37 | B — Bank degradation |
| MULTI_RAIL_OUTAGE_FAIL | 2 | C — Payment rail outage |

**All 6 PRD §32 scenarios are represented** (every category above is non-zero). Scenario C (multi-rail outage) is genuinely thin — 2 payments — which is expected given its default config (a 90-minute window right at simulation start, only 2 of 6 banks affected), not a generation bug; anything downstream that depends on Scenario C specifically (e.g. a per-scenario breakdown in the eval report) should note this sample size explicitly rather than treat it as statistically robust.

**Observed failure_class (noisy telemetry, the 950 failed payments):**

| `failure_class` | Count |
|---|---|
| TEMPORARY | 469 |
| PERMANENT | 179 |
| CUSTOMER_SPECIFIC | 153 |
| UNKNOWN | 129 |
| SYSTEMIC | 20 |

All 5 `ObservedFailureClass` values represented, including the ambiguity-injected `UNKNOWN` bucket (`test_all_six_scenarios_and_ambiguity_represented`'s exact requirement).

**Method mix:** upi 5,700 (57.0%) / card 2,210 (22.1%) / netbanking 1,470 (14.7%) / wallet 620 (6.2%) — consistent with `calibration/parameters.yaml`'s method-share weights (the one calibration binding confirmed genuinely wired, per gaps.md §C.3).

**Overall failure rate:** 950 / 10,000 = 9.5% — within the simulator's own asserted bound `[0.05, 0.30]` (`test_scenario_generators_produce_expected_failure_rate`).

## Non-circularity and reproducibility, re-confirmed against THIS run's seed (not trusted from a prior run)

- `test_ground_truth_not_derivable_from_visible_features`: **PASSED**. Direct re-derivation:
  `logistic_regression_auc=0.7264`, `random_forest_auc=0.7078`, `gbdt_auc=0.6941`,
  `permutation_baseline_auc=0.5258`, **max_auc=0.7264** — comfortably under the 0.85 leakage
  ceiling and above the 0.52 non-triviality floor.
- `test_deterministic_seed_reproducibility` (seed=42, n=1000, re-run independently): `is_identical=True`, `diff_count=0` — byte-identical, confirming this seed/config genuinely reproduces.

## Standing instruction

**This exact dataset (`simulation_id=b5345e16-0670-5c0f-bc83-c449e1f4a576`, generated
`2026-08-26T06:42:07Z`) is what every subsequent Phase 8 step runs against. No swapping mid-phase
— if this database needs to be regenerated for any reason, this file must be regenerated
alongside it, not edited in place.**
