# RecoveryOS — Addendum: Gaps Resolved & Risk Hardening

Companion to PRD.md, TRD.md, RecoveryOS_BuildPrompts.md. This file is the single source of
truth for the four open items — if any earlier doc says something different, this file wins.

---

## PART A — Gaps Resolved

### A.1 Customer Opt-Outs

**Decision: dual-path — real webhook endpoint AND simulator-generated, same table, same code path.**

You need both because the demo needs live opt-outs to be dramatic (someone taps "stop
contacting me" mid-demo and the system visibly respects it), and the 10k eval dataset needs
a realistic baseline opt-out rate to make `OptOutRule` a genuinely exercised policy branch,
not dead code.

**Endpoint:**
```
POST /v1/customers/{customer_id}/opt-out
  body: { reason?: string, channel?: "sms"|"email"|"support_call" }
  → 200 { customer_id, opted_out_at }

  Idempotent: re-calling on an already-opted-out customer returns 200 with the original
  opted_out_at, does not error and does not overwrite the timestamp.
```

**Implementation:**
- Writes `customers.opted_out_at = now()` — this column already exists in TRD §2, no schema
  change needed.
- Also writes an `events` row with `event_type = CUSTOMER_OPTED_OUT` so it shows up in that
  customer's timeline in the Audit Explorer.
- `OptOutRule` in the policy engine reads `customer.opted_out_at IS NULL` — already spec'd in
  TRD §3.4, no change needed there either.

**Simulator generation (Phase 1):**
- Add `opt_out_probability` param to `CustomerGenerator` (default 4%, configurable per scenario
  run) — a customer who has 2+ failed recovery attempts gets opted out with elevated probability
  (models real annoyance-driven opt-out), everyone else at baseline rate.
- This must call the SAME `POST /v1/customers/{id}/opt-out` endpoint via internal HTTP call
  during simulation, not a raw DB write — this guarantees the endpoint is exercised by the 10k
  eval run and isn't a demo-only code path that's untested at scale.

**Test to add to Phase 1 / Phase 11:**
```
test_opt_out_endpoint_is_idempotent()
test_opted_out_customer_never_receives_further_intervention() — end-to-end: opt a customer out
  mid-recovery-workflow, assert the NEXT policy check for their payment returns BLOCK via
  OptOutRule, not any other rule
test_simulator_and_live_endpoint_share_same_code_path() — assert simulator's opt-out calls hit
  the router function directly (import check), not a DB-write shortcut
```

---

### A.2 Action Cost Configuration

**Decision: configurable DB table, merchant-scoped, with a hardcoded seed default — not
hardcoded constants in code.**

Reasoning: PRD §53 already lists "merchant-specific policies" as a named stretch goal, and
EVI is the single most judge-scrutinized formula in the system — if costs are hardcoded,
the first "what if a merchant wants different SMS pricing" question exposes an unfinished
design. Making it a table costs ~20 minutes now and closes that gap permanently.

**Schema (add to Phase 0 migration set):**
```sql
CREATE TABLE action_costs (
    action_cost_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id      UUID REFERENCES merchants(merchant_id), -- NULL = platform default
    action_type      TEXT NOT NULL,        -- RETRY_NOW|RETRY_LATER|ALT_ROUTE|REMINDER|ESCALATE|DO_NOTHING
    cost_paise       BIGINT NOT NULL DEFAULT 0,
    friction_base_paise BIGINT NOT NULL DEFAULT 0,  -- base friction penalty before customer-type adjustment
    version          INT DEFAULT 1,
    effective_from   TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX idx_action_cost_merchant_action ON action_costs(COALESCE(merchant_id, '00000000-0000-0000-0000-000000000000'), action_type, version);
```

**Resolution logic in `services/recovery_engine/evi.py`:**
```python
def get_action_cost(merchant_id: UUID, action_type: str) -> ActionCost:
    row = db.query_action_cost(merchant_id=merchant_id, action_type=action_type)
    if row is None:
        row = db.query_action_cost(merchant_id=None, action_type=action_type)  # platform default
    return row
```

**Seed defaults (platform default row, merchant_id=NULL), from PRD's illustrative figures —
label these explicitly as "starting assumptions, tune from real data":**
```
RETRY_NOW    cost=0,    friction_base=10
RETRY_LATER  cost=0,    friction_base=20 (updated by migration 0009 — see §C.1: a zero
                                          friction made RETRY_LATER strictly cheaper than
                                          RETRY_NOW with equal probability outside an anomaly,
                                          so it won every payment in the Phase 5 sanity check
                                          regardless of whether waiting genuinely helped)
ALT_ROUTE    cost=200   (₹2, gateway fee estimate), friction_base=50
REMINDER     cost=20    (₹0.20 SMS), friction_base=30
ESCALATE     cost=15000 (₹150 labor-equivalent), friction_base=100
DO_NOTHING   cost=0,    friction_base=0
```

**Test to add to Phase 5:**
```
test_evi_uses_db_action_cost_not_hardcoded_constant() — change a cost row in the DB, assert
  EVI output for a fixed payment changes correspondingly, proving no hardcoded fallback exists
  in the code path
test_merchant_specific_cost_overrides_platform_default()
test_missing_merchant_cost_falls_back_to_platform_default_not_error()
```

---

### A.3 Fallback Diagnosis Schema

**Decision: exact schema below, produced by a deterministic lookup table, structurally
identical shape to the AI Diagnoser's real output — so downstream code (Recovery Engine,
Audit Explorer) never has to special-case "was this AI or fallback."**

```json
{
  "diagnosis_id": "uuid",
  "payment_id": "uuid",
  "cohort_id": "uuid | null",
  "root_cause": "string, from a closed enum — see mapping table below",
  "confidence": 0.55,
  "evidence": [
    { "fact": "failure_code=TIMEOUT", "source": "payment_metadata" },
    { "fact": "fallback_triggered=true, reason=ai_diagnoser_timeout", "source": "system" }
  ],
  "model_version": "fallback-rule-v1",
  "is_fallback": true,
  "created_at": "iso8601"
}
```

**Key differences from real AI output, both enforced at the schema level (same Pydantic
model, `is_fallback` and `model_version` are the only fields that vary in meaning):**
- `confidence` is capped at 0.6 max regardless of the rule matched — fallback should never
  claim higher certainty than a rule-table lookup deserves. This cap is a hardcoded constant
  documented here, not tunable per merchant.
- `evidence` always includes the `fallback_triggered=true` system fact so the Audit Explorer
  can visually flag "this decision used the deterministic fallback path," which is itself a
  good demo/interview talking point ("here's a decision made with zero LLM involvement, and
  you can see the system labels that transparently").

**Deterministic mapping table (`services/diagnosis_engine/fallback_rules.py`):**
```python
FALLBACK_MAP = {
    "TIMEOUT":          ("temporary_bank_degradation", 0.55),
    "BANK_DOWN":        ("systemic_degradation",        0.60),
    "INVALID_CREDS":    ("permanent_failure",            0.60),
    "INSUFFICIENT_FUNDS": ("customer_specific",           0.50),
    "EXPIRED_INSTRUMENT": ("permanent_failure",           0.55),
    # default / unrecognized failure_code:
    "_DEFAULT":         ("unknown", 0.30),
}
```

**Test to add to Phase 4:**
```
test_fallback_output_matches_exact_schema() — Pydantic validates against the SAME model class
  used for real AI output
test_fallback_confidence_never_exceeds_cap() — sweep every entry in FALLBACK_MAP, assert <=0.6
test_fallback_flagged_visibly_in_audit_explorer() — E2E, trigger fallback, assert UI shows the flag
test_unrecognized_failure_code_maps_to_unknown_not_a_guess() — proves the system abstains
  rather than fabricating certainty (this is your PRD §36 "abstention" requirement, made concrete)
```

---

## PART B — Implementation Risk Hardening

Each risk below gets: **why it's catastrophic**, **the specific code pattern that causes it**,
and **the exact test that proves it isn't happening** — add these to the phase noted.

### B.1 Metric Contamination (ground_truth_recoverable leakage)

**Why catastrophic:** this single leak invalidates every number in Phase 8. Not "weakens" —
invalidates. A model that can see the answer will show a suspiciously perfect incremental
revenue number, and any technical reviewer who asks "how is your feature pipeline separated
from ground truth" and gets a vague answer will (correctly) stop trusting the whole project.

**The exact code pattern that causes it (watch for this specifically):**
```python
# DANGEROUS — looks innocent, is not:
def get_payment_features(payment_id):
    payment = db.query("SELECT * FROM payments WHERE payment_id = %s", payment_id)
    # payment now includes ground_truth_recoverable as a dict key even if unused —
    # if this dict is ever passed wholesale into a feature vector builder or logged/
    # cached and later reused, the leak happens silently
    return build_features(payment)
```

**Hardening (add to Phase 0 + re-verify in Phase 5 and Phase 8):**
1. `SELECT *` is BANNED in any code path reachable from the inference/training pipeline —
   enforce with a `ruff`/custom lint rule or a code-review checklist grep:
   `grep -rn "SELECT \*" services/recovery_engine models/ services/diagnosis_engine` must
   return zero hits in CI.
2. Feature-building functions must take an explicit allow-list of columns as a constant at
   the top of the file, e.g. `ALLOWED_FEATURE_COLUMNS = [...]` (the exact TRD §3.3 list) —
   any DB row dict passed in gets filtered through this allow-list before touching the model.
3. `diagnoser_role` and a new `inference_role` (same restrictions) — both get zero SELECT
   grant on `ground_truth_recoverable` and the entire `simulator_latent_state` table, enforced
   at the Postgres GRANT level (belt-and-suspenders on top of the Python-level allow-list).

**Test (Phase 0, re-run in Phase 5 & Phase 8 CI):**
```
test_no_select_star_in_inference_reachable_code() — static grep-based CI check
test_inference_role_select_ground_truth_raises() — already exists from Phase 1, re-assert here
test_feature_vector_only_contains_allowed_columns() — for 100 random payments, build feature
  vectors, assert every key is in ALLOWED_FEATURE_COLUMNS, nothing extra sneaks in
test_model_auc_does_not_suspiciously_spike_after_feature_changes() — regression guard: keep a
  recorded "expected AUC range" from the honest Phase 5 run; if a future change pushes AUC
  above a suspiciously high ceiling (e.g. >0.97), CI fails and demands manual review before merge
```

---

### B.2 Idempotency Failures (double-execution — the catastrophic financial bug)

**Why catastrophic:** this is the one failure mode that would actually cost a merchant real
money in production — double-charging or double-crediting a recovery. It's also the single
most-asked "prove it" question from any fintech-adjacent reviewer.

**The exact code pattern that causes it:**
```python
# DANGEROUS — check-then-act race condition, the classic TOCTOU bug:
def execute(job):
    existing = db.get_recovery(idempotency_key=job.key)   # <- check
    if existing:
        return existing
    # ... time passes, another worker does the same check and also sees nothing ...
    result = provider.retry(job.payment_id)                # <- act, BOTH workers reach here
    db.upsert_recovery(job.key, result)
```
This is exactly why TRD §4.3 specifies the advisory lock WRAPS the check — if the lock isn't
literally around both the check and the act as one critical section, the guarantee is fake
even though the code "has" idempotency logic.

**Hardening (Phase 6, non-negotiable code review checklist item):**
1. The advisory lock acquisition MUST happen before the existence check, not after:
```python
def execute(job):
    with db.advisory_lock(job.key):        # lock FIRST
        existing = db.get_recovery(idempotency_key=job.key)
        if existing and existing.outcome is not None:
            return existing
        result = provider.retry(job.payment_id)
        db.upsert_recovery(job.key, result)
        return result
```
2. Additionally add a DB-level `UNIQUE` constraint on `recoveries.idempotency_key` (already
   in TRD §2 schema) as a hard backstop — even if the lock logic has a bug, a duplicate INSERT
   physically cannot succeed, it'll raise an IntegrityError that the worker catches and treats
   as "already handled, fetch existing."
3. Provider Adapter calls must themselves be wrapped so a network-level retry (e.g. HTTP
   client auto-retry on timeout) doesn't cause the SAME logical job to call `provider.retry()`
   twice even within one lock-held execution — pass a client-side idempotency header to
   Razorpay's API too (their test-mode API supports this), don't rely only on your own lock.

**Test (Phase 6, must be genuinely concurrent, not sequential-pretending-to-be-concurrent):**
```
test_two_real_threads_racing_same_idempotency_key_execute_provider_call_exactly_once() —
  use threading.Barrier to force both threads to hit the lock acquisition at the same
  instant, assert provider.retry call count == 1 via a call-counting mock/spy on the adapter
test_db_unique_constraint_backstop_rejects_duplicate_insert_even_if_lock_logic_is_bypassed() —
  deliberately bypass the lock in a test (call the raw upsert twice), assert the SECOND call
  raises IntegrityError and does not silently succeed
test_http_level_retry_does_not_cause_double_provider_call() — simulate a network blip that
  causes the HTTP client to auto-retry, assert Razorpay-side idempotency header prevents a
  second real charge/action
```

---

### B.3 Policy Engine Impurity (DB lookup sneaking into the rule loop)

**Why catastrophic:** breaks two claims simultaneously — the "100% unit-testable pure function"
credibility claim, AND the p99 <10ms latency target from TRD §8 (a DB round-trip per rule per
payment at scale turns a sub-millisecond in-memory check into a network-bound bottleneck).

**The exact code pattern that causes it:**
```python
# DANGEROUS — looks like a small, harmless addition, breaks purity:
class CooldownRule(PolicyRule):
    def check(self, payment, candidate, policy_config):
        last_attempt = db.query_last_attempt(payment.payment_id)  # <- I/O inside a "pure" rule
        return (now() - last_attempt) >= policy_config.retry_cooldown_hours
```
This is an easy trap because the natural instinct when writing a new rule is "I need X piece
of data, let me just fetch it" — the fix has to be structural, not just discipline.

**Hardening (Phase 5, structural not just convention):**
1. `PolicyRule.check(self, payment, candidate, policy_config)` signature takes ONLY these
   three already-hydrated dataclasses/Pydantic models — no `db`, no `session`, no `redis`
   object is ever passed to a rule or importable inside `services/policy_engine/rules.py`.
2. All data a rule could need (last_attempt_at, current anomaly severity for the payment's
   bank/method, customer opt-out status, etc.) gets pre-fetched ONCE by the caller
   (`evaluate()`'s orchestrating function, which is allowed to do I/O) and packed into the
   `payment`/`candidate` objects BEFORE any rule runs.
3. Enforce with a static check: `services/policy_engine/rules.py` must have zero imports of
   `db`, `sqlalchemy`, `redis`, `requests`, `httpx` — a CI grep/AST check, not just a docstring.

**Test (Phase 5):**
```
test_policy_engine_module_has_zero_forbidden_imports() — AST-parse rules.py, assert no
  import of db/sqlalchemy/redis/requests/httpx modules
test_policy_evaluate_runs_with_zero_db_queries() — wrap the DB session in a query-counting
  spy, call evaluate() with pre-hydrated inputs, assert query count == 0
test_policy_engine_p99_latency_under_10ms() — run evaluate() 10,000 times in a tight loop
  (no I/O), assert p99 wall-clock time meets the TRD §8 target; report the real number
```

---

### B.4 Floating Point Math in EVI / Ledger

**Why catastrophic:** silent, compounding, and exactly the kind of bug a fintech interviewer
will specifically probe for — "walk me through how you handle money" is a near-guaranteed
question, and "we use floats but round at the end" is a wrong answer.

**The exact code pattern that causes it:**
```python
# DANGEROUS:
def compute_evi(recovery_prob: float, amount_paise: int, cost_paise: int) -> float:
    expected_value = recovery_prob * amount_paise   # float * int = float, precision risk
    return expected_value - cost_paise               # returned as float, gets stored as such
```
Even though `amount_paise` is an integer, multiplying by a float probability produces a float,
and repeated arithmetic (subtracting friction, risk penalties, summing across the ledger) can
accumulate representation error — small per-payment, but visible once you sum 10,000 rows for
the headline "incremental recovered revenue" number, which is exactly the number you're most
scrutinized on.

**Hardening (Phase 5 + Phase 8):**
1. Use `decimal.Decimal` for the probability × amount step, or — simpler and faster — do the
   entire EVI calculation in **integer paise with a fixed-point probability** (e.g. probability
   scaled to an integer out of 10,000: `prob_bps = 8200` for 82.00%), avoiding floats entirely:
```python
def compute_evi_paise(recovery_prob_bps: int, amount_paise: int, cost_paise: int,
                       friction_paise: int, risk_paise: int) -> int:
    # all integer arithmetic, no float ever touches a money value
    expected_recovery = (amount_paise * recovery_prob_bps) // 10_000
    return expected_recovery - cost_paise - friction_paise - risk_paise
```
2. `recovery_prob_bps` (basis points, 0–10000) becomes the canonical representation the
   propensity model outputs and the DB stores — `candidate_actions.recovery_prob` in TRD §2
   should be reinterpreted/stored as this integer type, not `NUMERIC(5,4)` float-adjacent type
   (update the Phase 0 migration if not already applied).
3. Every SUM() in the evaluation harness (Phase 8) operates on BIGINT paise columns exclusively
   — the ONLY place a float/decimal is allowed to appear is in the final dashboard display
   layer, formatting paise-as-rupees for human eyes (`amount_paise / 100` for display only,
   never for storage or further computation).

**Test (Phase 5 + Phase 8):**
```
test_evi_calculation_uses_only_integer_arithmetic() — AST-check or type-check that no float
  literal or float() cast appears in evi.py
test_evi_no_rounding_drift_across_10000_summed_payments() — compute EVI for 10k synthetic
  payments, sum via integer arithmetic vs. a naive float reimplementation done ONLY in the
  test for comparison, assert the integer version has zero drift while documenting how much
  the float version would have drifted (a great "we caught this" artifact for your writeup)
test_ledger_sum_matches_sum_of_individual_rows_exactly() — SUM(actual_recovery_paise) in SQL
  must exactly equal a Python-side sum of the same rows, zero tolerance, not approx-equal
```

---

### C.1 Timing-Adjusted Recovery Probability — Coverage Limits (found in Phase 5)

**What was discovered:** building `services/recovery_engine/timing.py` (the mechanism behind
`RETRY_LATER`/`ALT_ROUTE` beating `RETRY_NOW` on probability, not just cost), Phase 1/2's episode
simulator turned out to have no data at the timescale that mechanism needs. Its retry-chain delay
is `MIN_RETRY_DELAY_SEC=60` to `MAX_RETRY_DELAY_SEC=300` (1-5 minutes,
`simulator/episodes/generator.py`) — nowhere near production's `retry_cooldown_hours=12` default.
The one time-decay curve that DOES exist in the simulator (`LatentRecoverabilityFunction`'s
customer-patience exponential decay, keyed on `attempt_number`) is explicitly latent ground truth —
using it in production inference would be the exact non-circularity leak Phase 1/2 exists to
prevent.

**What was built instead:** the only genuinely real, non-latent, already-measured "is right now
worse than normal" signal in the codebase is Phase 4's anomaly detector (`observed_rate` vs
`baseline_rate` per bank, `services/risk_engine/anomaly.py`). `timing.py` penalizes `RETRY_NOW`'s
base propensity by the ACTUAL measured success-rate ratio only during an active, sufficiently-
sampled, HIGH-severity systemic anomaly (matching TRD §3.2's own threshold) — clamped to `[0,
1.0]`, penalty only, never a boost. `RETRY_LATER`/`ALT_ROUTE` are left unadjusted under that same
condition, since they route around/wait out the exact thing being measured.

**Coverage limit — say this explicitly in any pitch/demo:** this mechanism only makes
`RETRY_LATER` win on probability during an active SYSTEMIC anomaly (TRD §3.1's own headline
example — "bank degradation, wait 12h, 73% recovery" — is inherently this case). PRD §32 Scenario
D (an individual customer's non-systemic temporary timeout recovering after a wait) is still
functionally unmodeled: outside a systemic anomaly, `RETRY_LATER` can only win the
next-best-action selection on cost/friction, never on a calibrated probability improvement,
because no such calibration data exists in Phase 1/2. If asked directly about the individual
timeout case, the honest answer is "not modeled yet — would need retry-chain simulation at an
hours scale, which Phase 1/2 doesn't generate," not an implied "yes, we know waiting helps in
general."

---

### C.2 Phase 2's Certified LightGBM Was Actually Selected on a Contaminated Split

**What was discovered:** re-auditing Phase 2 before building Phase 5's production adapter
(prompted by an explicit request to be thorough rather than trust the certificate), the
train/val/test splits were checked directly by set-comparing `episode_id` across the actual
parquet files — not by reading `phase_2_certificate.json`'s summary.

`simulator/dataset/builder.py` writes `train` and `val_temporal` as a genuine time-based split
of ONE `generate_episodes()` call (safe — verified zero overlap). But `val_random` and
`test_scenario` are each produced by a SEPARATE call to `build_simulator(seed=..., ...)`, which
constructs a fresh `DeterministicIdGenerator`/`SimRng` re-seeded from index 0 every time.
Episode generation replays its RNG stream deterministically, so the first N episodes of any two
calls sharing a seed are byte-identical.

Verified directly against the parquet files:
```
train vs val_random:        8,820 / 15,000 rows (58.8%) are verbatim duplicates of train rows
test_random vs test_scenario: 8,739 / 15,000 rows (58.3%) duplicated (doesn't touch train, but
                               means the "OOD scenario" split isn't an independent sample either)
train vs val_temporal:        0% overlap (this split is fine)
train vs test_random/test_temporal/test_scenario: 0% overlap (different seed, fine)
```

`models/recovery/train.py` uses `val_random` — 59% duplicated training rows — as the split it
picks the "best model" on. The certified claim ("LightGBM beats LR by 0.0401 AUC, clears the
>0.03 TRD §3.3 gate") was computed on that contaminated split.

**The real number**, from `models/recovery/artifacts/eval_test_temporal.json` — the only split
verified to have zero overlap with train:
```
LightGBM AUC = 0.8374  (95% CI: [0.8277, 0.8471])
LR      AUC = 0.8378
```
LR is marginally *higher*, and LightGBM's own point estimate sits inside its 95% CI around LR's
number — statistically indistinguishable, and LightGBM does not clear the >0.03 lift gate on
real held-out data. Per TRD §3.3's own rule, **the gate fails and Logistic Regression is the
correct certified default**, not LightGBM.

**Fix applied:** `services/recovery_engine/propensity.py` (Phase 5) loads `model_lr.pkl` +
`feature_transformer_v1.pkl`, not `model_lightgbm.txt`. No retraining — both artifacts already
existed from Phase 2; this only changes which one production actually uses.
`test_lgbm_does_not_beat_baseline_on_the_real_holdout_so_lr_stays_default` (tests/unit/test_propensity.py)
locks this in: it fails loudly if the artifacts are ever regenerated in a way that reverses it.

**Not yet fixed — and NOT a Phase 8 schedule blocker (resolved ambiguity, see below):** the root
cause in `simulator/dataset/builder.py`/`run_episode_mode` (re-seeding the same seed across
independent `generate_episodes()` calls) is still there.

**Why it doesn't block Phase 8:** TRD §7's evaluation harness computes its headline number
(`incremental_recovery`) via a raw SQL join over `recovery_ledger` and `baseline_runs` — tables
populated by running the LIVE pipeline against a canonical synthetic payment set, not by reading
`data/val_random`/`data/test_scenario` parquet files at all. Phase 8 never touches the
contaminated splits. The propensity model's own certification already correctly uses
`test_temporal` (verified zero overlap with train) as its reporting split, per gaps.md §C.2's fix.

**When it WOULD matter:** only if Phase 2's propensity model is ever retrained/re-certified again
in the future (a Phase 2 rerun, not a Phase 8 one) — that regeneration would reintroduce the same
`val_random`/`test_scenario` duplication and could silently re-flip the LR-vs-LightGBM gate
decision. Fix `simulator/dataset/builder.py` to draw an actual held-out subset from a single
larger generation run (instead of an independently re-seeded call) before anyone next runs
`models/recovery/train.py`, not before Phase 8.

---

## Summary — what changed in the build plan

| Item | Phase to update | New tables/files |
|---|---|---|
| Opt-out webhook | Phase 1, Phase 3, Phase 11 | `POST /v1/customers/{id}/opt-out` route |
| Action costs table | Phase 0 (schema), Phase 5 (EVI) | `action_costs` table |
| Fallback schema | Phase 4 | `fallback_rules.py`, shared Pydantic Diagnosis model |
| Ground-truth leak guard | Phase 0, Phase 5, Phase 8 (CI gate) | grep-based CI check + `inference_role` |
| Idempotency lock-order fix | Phase 6 | lock-before-check pattern, UNIQUE constraint |
| Policy engine purity gate | Phase 5 | AST-based CI check on `rules.py` imports |
| Integer-paise EVI | Phase 5, Phase 8 | `recovery_prob_bps` column type change |

All of the above should be applied to the existing phase prompts before you run them —
paste this addendum alongside PRD.md/TRD.md/BuildPrompts.md when handing phases to your agent.