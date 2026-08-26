# Phase 8 — PRD §36 AI Evaluation & §37 Adversarial Testing, Real Results

Computed against the canonical dataset ([phase8_canonical_run.md](phase8_canonical_run.md)),
using **app_role** — the only role permitted to see ground truth
(`simulator_latent_state.true_failure_type`/`true_recovery_prob_bps`). Confirmed live:
`inference` (the restricted, non-superuser login backing `inference_role`) gets
`permission denied for table simulator_latent_state` on the identical query — the live
inference/diagnoser path genuinely cannot see what this evaluation reads.

**Caveat on the app_role connection, stated honestly:** this dev environment's `recoveryos`
login (which `DATABASE_URL`/`get_app_session_factory()` connects as, i.e. "app_role" in this
app's own terms) is also flagged `rolsuper=true` in Postgres — it's the Docker-bootstrapped
superuser, not a role whose access is proven solely by GRANT statements. This is a pre-existing
dev-environment characteristic (`docker-compose.yml`'s `POSTGRES_USER: recoveryos`), not
something changed for this evaluation. The methodologically load-bearing part — that
`diagnoser_role`/`inference_role` cannot see ground truth — is proven by their own separate,
non-superuser logins failing with `permission denied`, not by this connection's restrictions.

## 1. Root-cause diagnosis accuracy

**No code-level mapping exists** between `simulator_latent_state.true_failure_type` (9 values)
and the diagnoser's `RootCause` enum (6 values) — this is a judgment call, documented here,
grounded in `simulator/outcomes/ground_truth.py`'s own `root_affinity` groupings:

| `true_failure_type` | Expected `RootCause` |
|---|---|
| `PERMANENT_INVALID_CREDS`, `PERMANENT_EXPIRED_INSTRUMENT`, `PERMANENT_ACCOUNT_CLOSED` | `permanent_failure` |
| `CUSTOMER_INSUFFICIENT_FUNDS` | `customer_specific` |
| `BANK_DEGRADATION_FAIL` | `temporary_bank_degradation` |
| `MULTI_RAIL_OUTAGE_FAIL` | `systemic_degradation` |
| `TEMPORARY_GATEWAY_TIMEOUT` | `temporary_bank_degradation` |
| `TRANSIENT_NETWORK_DROP` | `temporary_bank_degradation` (imperfect fit — the enum has no generic "transient/network" bucket distinct from bank-side degradation) |

**Raw accuracy: 213 / 950 = 22.42%.**

**This number is misleading read alone — the real signal is abstention, not wrongness.** Every
one of the 950 diagnoses went through the deterministic fallback path (no live LLM key this
run). Splitting the result:

| | Count | % of 950 |
|---|---:|---:|
| Abstained (`root_cause = unknown`) | 716 | 75.4% |
| Committed to a specific diagnosis | 234 | 24.6% |
| — of which correct | 213 | **91.03%** of committed diagnoses |
| — of which wrong | 21 | 8.97% of committed diagnoses |

PRD §36's own "Abstention" requirement is *"unknown situations should result in `UNKNOWN` rather
than fabricated certainty"* — by that standard, the fallback diagnoser is behaving as designed:
it commits only ~1 in 4 times, and when it commits, it's right ~91% of the time. The 22.42%
headline number conflates "abstained" with "wrong," which isn't a fair reading of what PRD §36
is actually asking to be measured. Reported as computed, not adjusted — but this context is
necessary to interpret it correctly.

## 2. Action-recommendation accuracy

**Ground-truth-optimal action** (also undefined in code) computed by re-running
`services/recovery_engine/evi.py`'s exact `calculate_evi()` formula per candidate, substituting
`true_recovery_prob_bps` for the model's estimated `recovery_prob_bps`, reusing each candidate's
own stored `cost_paise`/`friction_penalty_paise`/`risk_penalty_paise` (action/context-dependent,
not probability-dependent — valid to reuse unchanged). Selection rule mirrors
`next_best_action.py` exactly: argmax among non-`DO_NOTHING` candidates whose recomputed EVI is
`> 0` (the platform-default `min_expected_value_paise = 0`), else `DO_NOTHING`.

**Accuracy: 944 / 950 = 99.37%.**

- Ground-truth-optimal distribution: `RETRY_NOW` (944), `DO_NOTHING` (6).
- RecoveryOS's actual chosen action: `RETRY_NOW` (950/950 — every chosen candidate in this
  dataset was `RETRY_NOW`; no `RETRY_LATER`/`ALT_ROUTE`/`REMINDER`/`ESCALATE` was ever selected).
- The 6 misses are payments where the ground-truth probability was low enough that even the
  cheapest retry's expected value fell to/below zero, but the *model's estimated* probability
  (necessarily higher-variance, since it can't see ground truth) still cleared the floor.

## 3. PRD §37 Adversarial Testing — all 5 scenarios

| # | Scenario | Expected | Result | Evidence |
|---|---|---|---|---|
| 1 | Missing information (delete bank metadata) | Lower confidence / abstain | **PASS** | `tests/unit/test_diagnosis_adversarial.py::test_missing_bank_metadata_lowers_confidence`, `tests/unit/test_llm_diagnoser_guards.py::test_llm_path_missing_bank_metadata_lowers_confidence` |
| 2 | Conflicting information (bank healthy vs. provider degraded) | Investigate conflict | **PASS** | `test_diagnosis_adversarial.py::test_conflicting_signals_flagged_not_silently_resolved`, and **the S2 LLM-path re-confirmation**: `test_llm_diagnoser_guards.py::test_llm_path_conflicting_signals_flagged_not_silently_resolved` — this runs the identical adversarial input through `diagnose_with_llm()` itself (mocked LLM response), proving `apply_adversarial_guards()` fires on the real LLM-response path post-S2, not just the fallback path it used to be limited to |
| 3 | Systemic degradation (1000 failures, same bank) | Cohort-level diagnosis | **PASS** | `tests/integration/test_diagnosis_engine.py::test_systemic_degradation_produces_cohort_diagnosis` |
| 4 | Previously recovered payment | Stop unnecessary intervention | **FAIL (architectural gap, confirmed live)** | See below |
| 5 | Repeated failures | Escalation / stop | **PASS (proven live at real scale)** | See below |

All 8 tests for scenarios 1–3 re-run and pass (output captured this session). Scenarios 4 and 5
had **no existing test** (confirmed by search — neither is implemented anywhere in `tests/`),
so both were proven directly against the live containers and canonical data instead.

### Scenario 4 — FAIL, confirmed live

Picked a real payment from the canonical run that RecoveryOS had already successfully recovered
(`recoveries.outcome = 'SUCCESS'`). Published a **genuinely new** `PAYMENT_FAILED` event (fresh
`event_id`/`idempotency_key`, not a redelivery of the original) for that same payment through the
real ingest path. Result: a **second `diagnoses` row and a second `policy_decisions` row were
created** (verdict `BLOCK`) — the pipeline does not intrinsically know this payment was already
recovered.

The `BLOCK` only happened because `CooldownRule` (`services/policy_engine/rules.py:105-117`)
measures `now - last_attempt_at >= cooldown_hours` (12h default) — pure elapsed time, with **zero
awareness of the prior attempt's outcome**. Confirmed via `rule_trace`: `"elapsed=2:50:15 <
cooldown=12:00:00"`. `payments.status` is never updated to reflect a successful recovery anywhere
in the codebase (`grep` for any `UPDATE payments ... status` across `services/`/`workers/`
returns zero hits) — confirmed directly: the recovered payment's `payments.status` is still
`'failed'` after a real `SUCCESS` outcome.

**This means Scenario 4 passes only incidentally, only within the 12-hour cooldown window.** A
duplicate/resent failure event arriving *after* 12 hours for an already-successfully-recovered
payment would sail straight through `CooldownRule` (elapsed ≥ 12h) with no other rule checking
`recovery_ledger`/`recoveries` for a prior terminal `SUCCESS` — it would reach `ALLOW` and
genuinely re-execute a recovery attempt (a real duplicate customer contact / retry charge
attempt) on a payment that no longer needs it. Reported as found, not fixed — this is a real gap,
not a hypothetical one, and it's a policy-rule gap, not a naming/labeling gap.

### Scenario 5 — PASS, proven live at real scale

`RetryLimitRule` (`rules.py:120-138`) already implements this (`attempt_number <= max_retries`,
default `max_retries=2`, else verdict `ESCALATE`), with existing (non-adversarial-framed) unit
coverage. Since every payment in the natural canonical run only ever reached `attempt_number=1`
(single-pass batch, no live multi-day retry scheduling — same finding as the earlier "stopping
rule compliance" N/A result), this was proven directly: a synthetic prior `recoveries` row
(`attempt_number=2`, backdated 14 hours) was inserted for a real payment, then a fresh event was
published for it. Result, from the real `rule_trace`:

```
CooldownRule:   passed — "elapsed=14:00:14 >= cooldown=12:00:00"
RetryLimitRule: FAILED — "attempt_number=3 > max_retries=2"
→ verdict = ESCALATE
```

Exactly the expected behavior — confirmed against the live pipeline, not just a fixture. The
synthetic `recoveries` row and the resulting extra `diagnoses`/`candidate_actions`/
`policy_decisions` rows (from both scenario 4 and scenario 5's live proofs) were deleted
afterward, filtered by their exact synthetic `event_id`/`idempotency_key` — never by
`payment_id` — restoring the canonical dataset to its exact prior state (verified: all table
totals identical to the pre-test baseline).

## Standing note

Scenario 4's gap (no outcome-aware re-intervention guard, only a time-based cooldown) is a real,
confirmed finding from this evaluation pass — recorded here per this session's audit
methodology (report, don't fix without explicit go-ahead).
