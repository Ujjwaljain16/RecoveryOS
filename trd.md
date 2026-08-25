# RecoveryOS — Technical Requirements Document & System Design

**Razorpay Buildathon — Track 03: AI Revenue Recovery**
**Doc owner:** JainSahab | **Status:** Build-ready v1.0 | **Companion to:** RecoveryOS PRD

> Scope note: this TRD translates every PRD claim into a buildable spec — schemas, APIs, algorithms, state machines, and a phase-by-phase build order sized for a buildathon timeline. Nothing here is decorative; every section maps to a PRD requirement (§ refs below point back to PRD section numbers).

---

## 0. Document Map

| Section | Answers |
|---|---|
| 1. Architecture | What runs, where, and how it talks |
| 2. Data model | Every table, every column, every index |
| 3. Core algorithms | EVI formula, anomaly z-score, propensity model, policy DSL |
| 4. State machines | Payment lifecycle, recovery workflow, worker idempotency |
| 5. API contracts | Every endpoint, request/response shape |
| 6. Simulator design | How ground truth is generated and never leaked to the model |
| 7. Evaluation harness | How incremental revenue is computed without cheating |
| 8. NFRs | Latency, throughput, availability targets and how we hit them |
| 9. Security | Threat model + specific mitigations |
| 10. Observability | Metrics, traces, dashboards |
| 11. Deployment | Containers, CI/CD, environments |
| 12. Build plan | Hour-by-hour phases for the hackathon window |
| 13. Defensibility | What a judge/interviewer will try to break, and why it won't |

---

## 1. System Architecture

### 1.1 Design Principle

**Separation of cognition and control.** Everything that touches money is deterministic, testable, and replayable without an LLM in the loop. The LLM is a *reasoning and explanation layer* bolted onto a *rules engine* — never the actuator. This is the single architectural decision that makes the whole system defensible under adversarial questioning ("what if the LLM hallucinates a retry on a ₹5L payment?" → it can't, because the LLM never calls the executor; it only writes a recommendation record that the policy engine independently evaluates against hard-coded limits).

### 1.2 Component Diagram (concrete, deployable)

```
┌────────────────────────────────────────────────────────────────────┐
│                            Client Layer                             │
│   Next.js Dashboard (SSR) ──── WebSocket (live queue) ──── REST     │
└───────────────────────────────┬───────────────────────────────────┘
                                 │ HTTPS
┌────────────────────────────────▼───────────────────────────────────┐
│                          API Gateway (FastAPI)                      │
│   - AuthN/Z (API key per merchant, JWT for dashboard session)       │
│   - Rate limiting (per-merchant token bucket, Redis-backed)         │
│   - Request validation (Pydantic schemas, strict mode)              │
└───┬──────────────┬──────────────┬──────────────┬───────────────────┘
    │               │              │              │
    ▼               ▼              ▼              ▼
┌────────┐   ┌────────────┐  ┌───────────┐  ┌────────────┐
│ Event  │   │   Risk     │  │ Recovery  │  │ Evaluation │
│Ingest  │   │  Engine    │  │  Engine   │  │  Engine    │
│Service │   │(anomaly +  │  │(propensity│  │(baseline   │
│        │   │ diagnosis) │  │ + EVI +   │  │ comparison)│
│        │   │            │  │ next-best-│  │            │
│        │   │            │  │ action)   │  │            │
└───┬────┘   └─────┬──────┘  └─────┬─────┘  └─────┬──────┘
    │              │                │              │
    │        ┌─────▼─────┐          │              │
    │        │AI Diagnoser│          │              │
    │        │(LLM, async,│          │              │
    │        │ read-only) │          │              │
    │        └───────────┘           │              │
    │                                ▼              │
    │                        ┌───────────────┐      │
    │                        │ Policy Engine │      │
    │                        │ (pure func,   │      │
    │                        │  no I/O side  │      │
    │                        │  effects,     │      │
    │                        │  100% unit    │      │
    │                        │  testable)    │      │
    │                        └───────┬───────┘      │
    │                                ▼               │
    │                        ┌───────────────┐       │
    │                        │ Action Queue  │       │
    │                        │ (Redis + RQ / │       │
    │                        │  Celery,      │       │
    │                        │  delayed jobs)│       │
    │                        └───────┬───────┘       │
    │                                ▼               │
    │                        ┌───────────────┐       │
    │                        │ Outcome Worker│       │
    │                        │ (idempotent,  │       │
    │                        │  calls        │       │
    │                        │ Provider      │       │
    │                        │ Adapter)      │       │
    │                        └───────┬───────┘       │
    │                                ▼               │
    │                     ┌─────────────────────┐    │
    │                     │ PaymentProviderAdapter│  │
    │                     │  (interface) ──▶ Razorpay Test API / Simulator │
    │                     └─────────────────────┘    │
    ▼                                ▼               ▼
┌─────────────────────────────────────────────────────────┐
│                  PostgreSQL (primary store)               │
│  events | payments | recoveries | policy_decisions |      │
│  audit_log | recovery_ledger | anomalies | model_versions │
└─────────────────────────────────────────────────────────┘
              │
              ▼
     ┌──────────────────┐        ┌───────────────────┐
     │ Metrics (Prometheus)│───▶│  Grafana Dashboard │
     └──────────────────┘        └───────────────────┘
```

### 1.3 Why each boundary exists

- **Event Ingest ⟂ Risk Engine**: ingestion must never block on ML/LLM latency. Ingest writes to Postgres + emits to Redis stream; Risk Engine consumes asynchronously. This means a burst of 10k failure events doesn't stall the write path.
- **AI Diagnoser is a leaf node, not a hub**: it's called by Risk Engine, returns a structured `Diagnosis` object, and has zero write access to `payments`, `policy_decisions`, or `recovery_ledger`. This is enforced at the DB-permission level (separate read-only Postgres role for the diagnoser process), not just in application code — so it's a real boundary, not a convention.
- **Policy Engine is a pure function**: `evaluate(payment, action, context) -> Decision`. No network calls, no DB writes inside it. This makes it exhaustively unit-testable (every rule × every edge case) and means the audit trail can literally replay the function offline to prove a decision was correct.
- **Provider Adapter interface**: `class PaymentProvider(Protocol): def retry(...) -> Outcome`. Two implementations ship: `RazorpayTestAdapter` and `SimulatorAdapter`. Swapping between "demo against real Razorpay test mode" and "run 10k-payment evaluation against the simulator" is a one-line config change, not a code fork.

### 1.4 Data flow for one payment failure (concrete trace)

```
1. PAYMENT_FAILED event lands → Event Ingest writes to `events` table, publishes to `stream:payment_failed`
2. Risk Engine consumer picks it up (<200ms) → computes revenue-at-risk = amount × P(recover)
3. Risk Engine checks rolling anomaly window for (bank, method, time_bucket) → z-score
4. If z-score > threshold → mark as SYSTEMIC, attach cohort_id, suppress individual retries for cohort
5. AI Diagnoser called async with structured context (never raw PII) → returns {root_cause, confidence, evidence[]}
6. Recovery Engine generates candidate actions, scores each via EVI formula
7. Policy Engine evaluates top action against merchant's policy config → ALLOW / BLOCK / ESCALATE
8. If ALLOW → job pushed to Action Queue with idempotency_key = recovery:{payment_id}:{action}:{attempt_n}
9. Outcome Worker picks up job at scheduled time → calls Provider Adapter → gets outcome
10. Outcome written to `recoveries` + `recovery_ledger` (with counterfactual baseline outcome, from simulator ground truth)
11. Audit Log row written referencing every ID in steps 1–10 (immutable, insert-only table)
12. Metrics emitted at each stage; Grafana panel updates in real time
```

---

## 2. Data Model (PostgreSQL)

Design goals: append-mostly for auditability, explicit foreign keys everywhere (no orphaned decisions), every money column in paise (integer) to avoid float errors.

```sql
-- ═══════════════════════════════════════════════════════════
-- CORE ENTITIES
-- ═══════════════════════════════════════════════════════════

CREATE TABLE merchants (
    merchant_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name             TEXT NOT NULL,
    policy_config_id UUID REFERENCES policy_configs(policy_config_id),
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE customers (
    customer_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id      UUID NOT NULL REFERENCES merchants(merchant_id),
    is_returning     BOOLEAN DEFAULT false,
    lifetime_value_paise BIGINT DEFAULT 0,
    opted_out_at     TIMESTAMPTZ,          -- NULL = not opted out
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE payments (
    payment_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id      UUID NOT NULL REFERENCES merchants(merchant_id),
    customer_id      UUID NOT NULL REFERENCES customers(customer_id),
    amount_paise     BIGINT NOT NULL CHECK (amount_paise > 0),
    method           TEXT NOT NULL,        -- upi | card | netbanking | wallet
    bank             TEXT,
    status           TEXT NOT NULL,        -- created|authorized|failed|success|expired
    failure_code     TEXT,                 -- TIMEOUT|INVALID_CREDS|BANK_DOWN|...
    failure_class    TEXT,                 -- TEMPORARY|PERMANENT|CUSTOMER_SPECIFIC|SYSTEMIC|UNKNOWN
    is_synthetic     BOOLEAN DEFAULT true, -- false only for real test-mode calls
    ground_truth_recoverable BOOLEAN,      -- simulator only, NEVER exposed to inference path
    created_at       TIMESTAMPTZ DEFAULT now(),
    failed_at        TIMESTAMPTZ
);
CREATE INDEX idx_payments_merchant_status ON payments(merchant_id, status);
CREATE INDEX idx_payments_bank_method_time ON payments(bank, method, failed_at);

CREATE TABLE events (
    event_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id       UUID NOT NULL REFERENCES payments(payment_id),
    event_type       TEXT NOT NULL,        -- PAYMENT_CREATED|PAYMENT_FAILED|RETRY_EXECUTED|...
    payload          JSONB NOT NULL,
    occurred_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_events_payment ON events(payment_id, occurred_at);

-- ═══════════════════════════════════════════════════════════
-- RISK & ANOMALY
-- ═══════════════════════════════════════════════════════════

CREATE TABLE anomaly_windows (
    window_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type       TEXT NOT NULL,        -- bank|method|merchant
    scope_entity     TEXT NOT NULL,
    time_bucket      TIMESTAMPTZ NOT NULL, -- 15-min bucket
    baseline_rate    NUMERIC(5,4),
    observed_rate    NUMERIC(5,4),
    z_score          NUMERIC(6,3),
    severity         TEXT,                 -- low|medium|high
    is_anomaly       BOOLEAN,
    created_at       TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX idx_anomaly_scope_bucket ON anomaly_windows(scope_type, scope_entity, time_bucket);

CREATE TABLE diagnoses (
    diagnosis_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id       UUID REFERENCES payments(payment_id),
    cohort_id        UUID,                 -- NULL if isolated, set if part of systemic event
    root_cause       TEXT NOT NULL,
    confidence       NUMERIC(4,3),
    evidence         JSONB NOT NULL,       -- structured facts the LLM cited, for grounding checks
    model_version     TEXT NOT NULL,
    created_at       TIMESTAMPTZ DEFAULT now()
);

-- ═══════════════════════════════════════════════════════════
-- RECOVERY DECISIONING
-- ═══════════════════════════════════════════════════════════

CREATE TABLE candidate_actions (
    candidate_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id        UUID NOT NULL REFERENCES payments(payment_id),
    action_type       TEXT NOT NULL,       -- RETRY_NOW|RETRY_LATER|ALT_ROUTE|REMINDER|ESCALATE|DO_NOTHING
    recovery_prob     NUMERIC(5,4) NOT NULL,
    expected_value_paise BIGINT NOT NULL,  -- can be negative
    cost_paise        BIGINT DEFAULT 0,
    friction_penalty_paise BIGINT DEFAULT 0,
    risk_penalty_paise BIGINT DEFAULT 0,
    model_version     TEXT NOT NULL,
    created_at        TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE policy_configs (
    policy_config_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    max_retries      INT DEFAULT 2,
    retry_cooldown_hours INT DEFAULT 12,
    max_amount_paise BIGINT DEFAULT 2500000,
    stop_after_success BOOLEAN DEFAULT true,
    stop_after_opt_out BOOLEAN DEFAULT true,
    escalate_after_failures INT DEFAULT 2,
    min_expected_value_paise BIGINT DEFAULT 0, -- floor for "do nothing" trigger
    version          INT DEFAULT 1,
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE policy_decisions (
    decision_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id       UUID NOT NULL REFERENCES payments(payment_id),
    candidate_id     UUID NOT NULL REFERENCES candidate_actions(candidate_id),
    policy_config_id UUID NOT NULL REFERENCES policy_configs(policy_config_id),
    verdict          TEXT NOT NULL,        -- ALLOW|BLOCK|ESCALATE
    rule_trace       JSONB NOT NULL,       -- ordered list of {rule, passed, reason}
    created_at       TIMESTAMPTZ DEFAULT now()
);

-- ═══════════════════════════════════════════════════════════
-- EXECUTION & OUTCOME
-- ═══════════════════════════════════════════════════════════

CREATE TABLE recoveries (
    recovery_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id        UUID NOT NULL REFERENCES payments(payment_id),
    decision_id       UUID NOT NULL REFERENCES policy_decisions(decision_id),
    idempotency_key   TEXT NOT NULL UNIQUE,
    attempt_number    INT NOT NULL,
    action_type       TEXT NOT NULL,
    scheduled_for     TIMESTAMPTZ NOT NULL,
    executed_at       TIMESTAMPTZ,
    outcome           TEXT,                -- SUCCESS|FAILED|PENDING
    recovered_amount_paise BIGINT DEFAULT 0,
    provider_ref      TEXT,
    stopping_rule_triggered TEXT,          -- NULL or e.g. 'MAX_RETRIES'
    created_at        TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_recoveries_payment ON recoveries(payment_id);

CREATE TABLE recovery_ledger (
    ledger_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id        UUID NOT NULL REFERENCES payments(payment_id),
    revenue_at_risk_paise BIGINT NOT NULL,
    expected_recovery_paise BIGINT NOT NULL,
    actual_recovery_paise BIGINT DEFAULT 0,
    baseline_outcome  TEXT,                -- from simulator ground truth: what baseline strategy would've gotten
    incremental_recovery_paise BIGINT DEFAULT 0,
    intervention_cost_paise BIGINT DEFAULT 0,
    net_recovery_paise BIGINT DEFAULT 0,
    created_at        TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE audit_log (
    audit_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id        UUID REFERENCES payments(payment_id),
    diagnosis_id      UUID REFERENCES diagnoses(diagnosis_id),
    candidate_id      UUID REFERENCES candidate_actions(candidate_id),
    decision_id       UUID REFERENCES policy_decisions(decision_id),
    recovery_id       UUID REFERENCES recoveries(recovery_id),
    summary           TEXT NOT NULL,       -- human-readable one-liner for the audit explorer
    created_at        TIMESTAMPTZ DEFAULT now()
);
-- audit_log is INSERT-ONLY. No UPDATE/DELETE grants for the application role.
```

**Immutability enforcement:** revoke `UPDATE`, `DELETE` on `audit_log` and `events` from the app DB role at the Postgres grant level, not just app-layer discipline — so even a bug can't silently rewrite history. This is a concrete, demoable "we thought about tamper-resistance" talking point.

---

## 3. Core Algorithms

### 3.1 Expected Recovery Value (EVI)

```
EVI(payment, action) =
    P(recover | payment, action) × amount
    − cost(action)
    − friction_penalty(action, customer)
    − risk_penalty(action, context)
```

- `P(recover | ...)` comes from the Recovery Propensity Model (§3.3).
- `cost(action)`: fixed per action type (SMS ≈ ₹0.20, gateway retry call ≈ ₹0, human escalation ≈ ₹150 labor-equivalent — configurable table).
- `friction_penalty`: scaled by customer opt-out risk; returning customers get lower friction penalty for reminders, first-time customers get higher (avoid annoying new users into churn).
- `risk_penalty`: nonzero only for actions during a SYSTEMIC anomaly window — retrying into a degraded bank is penalized to bias toward `RETRY_LATER`/`DO_NOTHING`.

Decision rule: `select action = argmax(EVI) subject to EVI > policy.min_expected_value_paise`. If no action clears the floor → `DO_NOTHING` (§9 PRD — this is the "deliberate non-intervention" differentiator, and it's just a threshold check, which is exactly why it's defensible: it's not vibes, it's one line of code a judge can read.)

### 3.2 Anomaly Detection (systemic degradation)

Rolling z-score per `(bank, method)` over 15-minute buckets, baseline from trailing 7-day same-hour average:

```
z = (observed_failure_rate − baseline_failure_rate) / baseline_std_dev
```

- `z > 3` → `severity: high`, `is_anomaly: true` → cohort formed, individual retries suppressed for that cohort until re-evaluation window passes.
- `z ∈ (2, 3]` → `severity: medium` → flagged, no auto-suppression, dashboard alert.
- Minimum sample size guard: n < 30 in bucket → skip z-score, mark `insufficient_data` (prevents false positives on low-traffic banks).

This is intentionally a classical statistical control-chart method, not an ML model — deterministic, explainable in one sentence to a judge, and doesn't require training data to bootstrap. (Complexity budget goes to the propensity model instead, where it earns its keep.)

### 3.3 Recovery Propensity Model

**MVP choice: gradient-boosted trees (LightGBM), not a neural net.** Rationale to state explicitly in the pitch: tabular data, small feature set, need per-prediction feature-importance for the audit trail ("why 82%?" must be answerable), and training data volume (10k synthetic payments) doesn't justify anything heavier.

Features:
```
amount_paise, method, bank, failure_code, failure_class,
customer.is_returning, customer.lifetime_value_paise,
hours_since_failure, attempt_number,
bank_current_success_rate (from anomaly_windows),
day_of_week, hour_of_day,
customer_prior_recovery_rate (historical, leakage-safe: only prior payments, never this one)
```

Target: `recovered` (binary), from simulator ground truth during training; at inference time the model never sees `ground_truth_recoverable` — that column exists solely for post-hoc evaluation (§7), enforced by giving the inference service a Postgres role with no SELECT grant on that column.

Baseline-before-ML discipline: ship a logistic regression first, gate the LightGBM upgrade behind an AUC improvement check (>0.03 AUC lift on held-out fold) before it's allowed to replace it in the default config. This mirrors the "start simple, justify complexity with data" principle already established in prior JainSahab projects (mastery engine, LTR model).

### 3.4 Policy Engine — Rule DSL

Rules are declarative, ordered, and short-circuit on first `BLOCK`:

```python
RULES: list[PolicyRule] = [
    EligibilityRule(),          # payment.status == 'failed' and not expired
    OptOutRule(),                # customer.opted_out_at is None
    CooldownRule(),               # now - last_attempt >= cooldown_hours
    RetryLimitRule(),             # attempt_number <= max_retries
    AmountLimitRule(),            # amount_paise <= max_amount_paise
    SystemicSuppressionRule(),    # if cohort is SYSTEMIC and action == RETRY_NOW -> BLOCK, suggest RETRY_LATER
    MinExpectedValueRule(),       # EVI > min_expected_value_paise
]

def evaluate(payment, candidate, policy_config) -> PolicyDecision:
    trace = []
    for rule in RULES:
        result = rule.check(payment, candidate, policy_config)
        trace.append({"rule": rule.name, "passed": result.passed, "reason": result.reason})
        if not result.passed:
            verdict = "ESCALATE" if rule.escalates_on_fail else "BLOCK"
            return PolicyDecision(verdict, trace)
    return PolicyDecision("ALLOW", trace)
```

Every rule is a pure function with 100% branch coverage in unit tests — this table of rules × test cases is itself a strong artifact to show in an interview ("here's my policy engine test matrix, 7 rules × 4 edge cases each = 28 deterministic tests, all green").

---

## 4. State Machines

### 4.1 Payment lifecycle

```
CREATED → AUTHORIZED → SUCCESS
        ↘ FAILED → [diagnosed] → [recovery attempts] → SUCCESS
                                                       ↘ EXPIRED / STOPPED
```

### 4.2 Recovery workflow (per payment)

```
FAILED
  │
  ▼
DIAGNOSING ──(AI timeout)──▶ FALLBACK_DIAGNOSIS (deterministic rule: failure_code → default class)
  │
  ▼
SCORING (candidate actions generated + EVI computed)
  │
  ▼
POLICY_CHECK ──BLOCK──▶ STOPPED(reason)
  │
 ALLOW
  │
  ▼
SCHEDULED (job in Action Queue, idempotency_key set)
  │
  ▼
EXECUTING ──(worker crash)──▶ requeued from persisted job state, same idempotency_key, no duplicate side-effect
  │
  ▼
VERIFYING
  │
  ├─SUCCESS──▶ STOPPED(reason='recovered') → recovery_ledger updated
  │
  └─FAILED──▶ attempt_number += 1
                 │
                 ├─ attempt_number > max_retries ──▶ ESCALATE or STOPPED(reason='max_retries')
                 └─ else ──▶ back to SCORING (re-evaluate EVI, since context may have changed)
```

Every arrow is a logged transition in `events`; the audit explorer (§48 PRD) is literally a query over this table joined to `audit_log`, not a separately-maintained view — meaning it can never drift out of sync with reality.

### 4.3 Idempotency guarantee

`idempotency_key = f"recovery:{payment_id}:{action_type}:{attempt_number}"`. Worker wraps execution in:

```python
def execute(job):
    existing = db.get_recovery(idempotency_key=job.key)
    if existing and existing.outcome is not None:
        return existing  # already done, return cached result, no re-execution
    with db.advisory_lock(job.key):  # Postgres advisory lock, prevents concurrent workers double-firing
        result = provider.retry(job.payment_id)
        db.upsert_recovery(job.key, result)
        return result
```

---

## 5. API Contracts (representative subset)

```
POST /v1/events
  → ingest a payment event
  body: { payment_id, merchant_id, customer_id, amount_paise, method, bank, event_type, failure_code?, idempotency_key? }
  → 202 { event_id }

GET /v1/risk/summary
  → { total_revenue_at_risk_paise, recoverable_estimate_paise, affected_payment_count }
  (merchant is the caller's own verified identity — see auth note below, not a query param)

GET /v1/payments/{payment_id}/detail
  → { payment, diagnosis, candidate_actions[], policy_decision, recovery_history[] }

POST /v1/simulate/degrade
  body: { bank, method, target_success_rate, duration_minutes }
  → triggers the "SIMULATE DEGRADATION" demo hook (§38 PRD), only enabled when ENV=demo

GET /v1/experiments/{run_id}
  → { baseline: {...}, recoveryos: {...}, incremental_recovery_paise, chart_data }

GET /v1/audit/{payment_id}
  → full decision chain, replayable

POST /v1/policy-configs
  → merchant-configurable policy (§53 PRD, merchant-specific policies stretch goal)
```

Auth (Task 4): every route above resolves the caller's identity from a verified `X-API-Key` (hashed lookup against `merchants.api_key_hash`), never from a client-supplied `merchant_id` in a header, query param, or body field. This is why `GET /v1/risk/summary` takes no `?merchant_id=` — the merchant is whichever identity the API key resolves to, not something the caller states. Where a request body still names `merchant_id` (e.g. `POST /v1/events`), it is checked *against* the verified identity (mismatch → 403), never trusted as the identity itself.

Idempotency: mutating endpoints that need a client-supplied idempotency key take it as a **request BODY field** (`idempotency_key`), not an `Idempotency-Key` header — deliberately: it names the specific logical event/action being retried, which is a property of that one request's payload, not something that applies to the whole HTTP request/connection the way a header-scoped value would. Falls back to a server-generated id if the caller omits it. All responses include `X-Model-Version` and `X-Policy-Version` headers so any dashboard screenshot is reproducible against the exact model/policy that produced it — useful for judges who ask "can you show me that again." Both headers are read live from the actual running model/policy version at response time (`apps/api/versioning.py`), not hardcoded.

---

## 6. Synthetic Merchant Environment (Simulator)

**Non-negotiable design rule: the simulator's ground truth (`ground_truth_recoverable`, `baseline_outcome`) must be generated by a process structurally independent from the inference pipeline's feature set**, or the evaluation is circular and the incremental-revenue number is fabricated-looking even if numerically real. Concretely:

- Ground truth is generated from a **latent recoverability function** with its own hidden parameters (e.g., customer patience curve, bank recovery half-life) that are *correlated with* but not *identical to* the visible features the model uses.
- Inference-time feature extraction reads only `payments`, `events`, `anomaly_windows` — never `ground_truth_recoverable` or the simulator's latent parameters. Enforced via a separate read-only DB role (§3.3).
- Failure scenarios A–F (§32 PRD) are implemented as pluggable `ScenarioGenerator` classes so new adversarial cases can be added without touching core simulator logic:

```python
class ScenarioGenerator(Protocol):
    def generate(self, n: int, clock: SimClock) -> list[PaymentEvent]: ...

SCENARIOS = {
    "normal": NormalFailureScenario(rate=0.03),
    "bank_degradation": BankDegradationScenario(bank="bank_x", spike_to=0.18, window_minutes=60),
    "rail_outage": MultiRailOutageScenario(affected_banks=["bank_x","bank_y"]),
    "temporary_timeout": TemporaryTimeoutScenario(recovers_after_hours=12),
    "permanent_failure": PermanentFailureScenario(),
    "customer_specific": CustomerRepeatFailureScenario(),
}
```

Volume target for the eval run: 10,000 payments, 2,000 customers, 3 merchants, 5 methods, mixed scenario weights matching §31–32 PRD.

---

## 7. Evaluation Harness

**Rule: the number reported must be computable by a third party from raw table dumps alone**, not trusted from an internal aggregate. So the evaluation query is a straight SQL join, not application code:

```sql
WITH recoveryos_result AS (
    SELECT payment_id, actual_recovery_paise FROM recovery_ledger
),
baseline_result AS (
    -- baseline strategy replayed against the SAME synthetic payment set,
    -- using the SAME ground truth, computed by a separate BaselineSimulator
    SELECT payment_id, recovered_amount_paise AS baseline_recovery_paise FROM baseline_runs
)
SELECT
    SUM(r.actual_recovery_paise)                         AS recoveryos_total,
    SUM(b.baseline_recovery_paise)                        AS baseline_total,
    SUM(r.actual_recovery_paise) - SUM(b.baseline_recovery_paise) AS incremental_recovery
FROM recoveryos_result r
JOIN baseline_result b USING (payment_id);
```

Secondary metrics (§34 PRD) all follow the same "raw SQL over immutable tables" pattern — this is a deliberate credibility choice: judges distrust dashboards that could be lying; they trust a query they can rerun.

Adversarial test suite (§37 PRD) ships as `tests/adversarial/`, run in CI on every commit, asserting behavioral properties (not just outputs): e.g. `test_missing_bank_metadata_lowers_confidence()`, `test_conflicting_signals_triggers_investigation_flag()`, `test_already_recovered_payment_never_reintervened()`.

---

## 8. Non-Functional Requirements

| Dimension | Target (MVP) | How achieved |
|---|---|---|
| Event ingest throughput | 500 events/sec sustained | Async write + Redis stream buffer, decoupled consumers |
| Diagnosis latency (p95) | < 3s | AI Diagnoser timeout at 2.5s → deterministic fallback |
| Policy check latency (p99) | < 10ms | Pure in-memory function, no I/O |
| Worker recovery time after crash | < 30s | Persisted job state in Redis/Postgres, requeue on restart |
| Duplicate action rate | 0% | Idempotency key + advisory lock (§4.3) |
| Audit trail completeness | 100% of decisions | Every state transition writes to `events`; CI test asserts no orphaned `policy_decisions` without `audit_log` row |
| Availability (demo day) | No single point of failure in critical path | Provider Adapter degrades to Simulator on Razorpay test-API outage; AI Diagnoser degrades to rule-based fallback |

**Scalability story beyond MVP** (for the "how would this scale to production" question): Postgres → read replicas for dashboard queries, partition `events`/`payments` by `merchant_id` + time; Action Queue → horizontally scale workers behind Redis Streams consumer groups; propensity model → move from synchronous scoring to a feature-store + batch-scored cache refreshed every N minutes for high-volume merchants, with online fallback for cold-start payments.

---

## 9. Security & Threat Model

| Threat | Mitigation |
|---|---|
| LLM prompt injection via failure metadata (e.g. malicious `failure_code` string) | AI Diagnoser only receives sanitized, schema-validated structured fields — never raw free-text fields concatenated into the prompt; output is schema-validated (Pydantic) before use, rejected if malformed |
| LLM recommends an out-of-policy action | Irrelevant by construction — LLM output is a `Diagnosis`, never an `Action`; only the Policy Engine can produce an `ALLOW` verdict, and it has zero LLM dependency |
| Duplicate/replayed webhook triggers double recovery | Idempotency key + Postgres advisory lock (§4.3) |
| Tampering with audit history | `REVOKE UPDATE, DELETE ON audit_log, events FROM app_role` at DB grant level |
| Over-limit transaction executed | `AmountLimitRule` hard block, tested with boundary values (exactly at limit, one paise over) |
| PII leakage into LLM context or logs | Customer PII (name, contact) never enters the diagnosis pipeline — only `customer_id`, `is_returning`, and aggregate stats are passed |
| Unauthorized merchant accessing another merchant's data | Row-level security policy on all merchant-scoped tables (`merchant_id = current_setting('app.current_merchant')`) |

---

## 10. Observability

Prometheus metrics (all from §40 PRD, with label cardinality kept low deliberately — `merchant_id` as a label only on aggregated rollups, not raw counters, to avoid cardinality blowup):

```
recovery_attempts_total{action_type}
recovery_success_total{action_type}
revenue_at_risk_paise_total
revenue_recovered_paise_total
incremental_revenue_paise_total
policy_blocks_total{rule}
stopping_rule_triggers_total{reason}
systemic_degradation_events_total{bank}
diagnosis_latency_seconds (histogram)
ai_diagnoser_fallback_total   -- tracks how often the LLM path degrades, an honest reliability signal
```

Grafana dashboard panels map 1:1 to the Control Tower UI (§44 PRD) so the same numbers a merchant sees are the same numbers ops sees — no separate "internal truth" vs "customer-facing truth" drift.

---

## 11. Deployment

```
docker-compose (dev/demo):
  - api (FastAPI)
  - worker (Celery/RQ, N replicas)
  - postgres
  - redis
  - dashboard (Next.js)
  - prometheus + grafana

Env separation:
  ENV=demo   → SimulatorAdapter default, /v1/simulate/degrade enabled
  ENV=staging → RazorpayTestAdapter, simulate endpoint disabled
```

CI (GitHub Actions): lint → unit tests (policy engine, EVI, anomaly z-score) → integration tests (full workflow against Postgres testcontainer) → adversarial suite → build images.

---

## 12. Build Plan (mapped from PRD §51, sized for a buildathon)

| Phase | Deliverable | Priority |
|---|---|---|
| 1. Simulator core | Merchant/customer/payment models + 3 scenario generators + 2k payments | P0 |
| 2. Baseline strategy | Fixed-interval retry, run against simulator, get baseline number | P0 |
| 3. Schema + ingest | All tables above, `/v1/events` working end-to-end | P0 |
| 4. Anomaly + policy engine | z-score detector + full rule DSL + unit test matrix | P0 |
| 5. Propensity model v0 | Logistic regression, feature pipeline, leakage-safe split | P0 |
| 6. EVI + next-best-action | Candidate generation + scoring + DO_NOTHING path | P0 |
| 7. Execution + idempotency | Worker, advisory lock, Provider Adapter (Simulator first) | P0 |
| 8. Evaluation harness | SQL-based incremental revenue query, full 10k run | P0 |
| 9. AI Diagnoser | LLM call with structured I/O + fallback path | P1 |
| 10. Dashboard | Control Tower, Payment Detail, Experiment screen | P1 |
| 11. Razorpay test-mode integration | RazorpayTestAdapter, swap-in demo | P1 |
| 12. Adversarial suite + hardening | §37 test cases, chaos: kill worker mid-job, verify no dup | P1 |
| 13. LightGBM upgrade | Only if AUC lift proven over logistic baseline | P2 (stretch) |
| 14. Merchant-specific policy configs | UI for policy tuning | P2 (stretch) |

P0 = MVP definition per PRD §52, must all be green before anything else is touched.

---

## 13. Defensibility — anticipated hard questions

**"Couldn't the LLM just be replaced with a lookup table?"**
No — root-cause reasoning over conflicting/missing signals (§37 adversarial cases) is exactly where an LLM adds value over a static rule table; the rule table is what handles execution, on purpose, and that split is demonstrable by showing the LLM boundary in code.

**"How do you know the incremental revenue number isn't just noise or a rigged simulator?"**
Ground truth generation is structurally decoupled from the inference feature set (§6), the eval query is raw SQL over immutable tables (§7), and the baseline is run through the identical pipeline shape (same event schema, same worker path) — only the decision logic differs.

**"What happens if this runs against real money and the model is wrong?"**
It can't cause direct harm beyond the configured policy envelope — `AmountLimitRule`, `RetryLimitRule`, and `min_expected_value_paise` are hard floors independent of model confidence, and every action is logged before execution with a replayable rule trace.

**"Why Postgres and not something 'more scalable' like a NoSQL store?"**
Because the core value proposition is auditability and financial correctness — ACID transactions and foreign-key integrity are load-bearing requirements here, not defaults. Scale is handled via partitioning/read-replicas (§8), not by giving up consistency.

---

## Appendix: One-sentence system summary for a resume/interview bullet

> *Designed and built RecoveryOS, an AI-assisted revenue recovery control plane with a strict cognition/control separation — LLM-based root-cause diagnosis feeding a deterministic, unit-tested policy engine — validated against a synthetic 10k-payment environment with leakage-safe ground truth, demonstrating measurable incremental recovered revenue over a baseline retry strategy with 100% idempotent, auditable execution.*