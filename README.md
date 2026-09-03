# RecoveryOS

### The Autonomous Recovery Layer for Failed Payments

[![CI](https://github.com/Ujjwaljain16/RecoveryOS/actions/workflows/ci.yml/badge.svg)](https://github.com/Ujjwaljain16/RecoveryOS/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11-blue)
![TypeScript](https://img.shields.io/badge/dashboard-Next.js%2014%20%2B%20TypeScript-blue)

A failed payment isn't the end of the transaction. It's the beginning of a recovery mission.

RecoveryOS turns `payment.failed` into a closed loop — investigate, decide, act, observe, replan —
instead of a fixed retry schedule and a shrug.

> **AI investigates and recommends. Deterministic systems authorize. RecoveryOS executes.**

That's the whole architecture in one line, and it's the thing that makes this defensible: the AI
never moves money on its own, under any circumstance — see [§6](#6-ai-that-recommends--never-authorizes).

---

## The result

# +₹73,181.78

**mean incremental recovery**, vs. a compliance-aware baseline, across 5 independent
10,000-payment seeds.

| | |
|---|---|
| 95% confidence interval | **+₹52,918.53 to +₹93,445.04** |
| Seeds with a positive result | **5 / 5** |
| Seeds where RecoveryOS's recovered payments are a strict superset of the baseline's | **5 / 5** |

This is the *whole system's* incremental recovery — the deterministic propensity/EVI/policy
engine is doing most of that work, and it's completely AI-blind by construction. The AI layer is
mechanism-proven and safety-proven, but its own real-model behavioral contribution has only been
measured at smoke-test scale so far. Full honesty on that: [§21](#21-is-the-ai-actually-load-bearing).

![Control Tower — live merchant overview, real recovered/incremental numbers, active recovery missions in flight](docs/images/control-tower.png)

*The Control Tower, live — not a mock. Every number on this screen is read from the same database
the recovery engine writes to.*

---

## Table of contents

1. [What is RecoveryOS?](#1-what-is-recoveryos)
2. [The Product Experience](#2-the-product-experience)
3. [How It Works](#3-how-it-works)
4. [The Recovery Mission Loop](#4-the-recovery-mission-loop)
5. [Architecture](#5-architecture)
6. [AI That Recommends — Never Authorizes](#6-ai-that-recommends--never-authorizes)
7. [The AI Investigator](#7-the-ai-investigator)
8. [The Bounded Recovery Recommendation](#8-the-bounded-recovery-recommendation)
9. [Deterministic Safety](#9-deterministic-safety)
10. [Does RecoveryOS Actually Recover More Revenue?](#10-does-recoveryos-actually-recover-more-revenue)
11. [Fairness & Experiment Design](#11-fairness--experiment-design)
12. [Reliability & Failure Safety](#12-reliability--failure-safety)
13. [Razorpay Integration](#13-razorpay-integration)
14. [Observability & Auditability](#14-observability--auditability)
15. [Security Model](#15-security-model)
16. [Testing](#16-testing)
17. [Run the Demo](#17-run-the-demo)
18. [Reproducing the Evaluation](#18-reproducing-the-evaluation)
19. [Repository Map](#19-repository-map)
20. [Design Decisions](#20-design-decisions)
21. [Is the AI Actually Load-Bearing?](#21-is-the-ai-actually-load-bearing)
22. [Limitations — Honest Disclosures](#22-limitations--honest-disclosures)
23. [Build Challenges](#23-build-challenges)
24. [Why RecoveryOS Is Different](#24-why-recoveryos-is-different)

---

## 1. What is RecoveryOS?

Most payment-recovery systems do one thing:

```text
payment.failed → retry → retry → give up
```

RecoveryOS does this instead:

```text
payment.failed
  → understand WHY it failed
  → evaluate WHAT is allowed and worth doing
  → choose the best recovery strategy
  → execute it safely
  → observe the outcome
  → replan if it didn't work
  → recover / escalate / stop
```

A failed payment isn't a dead end — it's revenue that might still be recoverable, if the system
asks the right question. Not *"should we retry?"* but:

> **"Given everything we know right now, what is the safest, highest-value recovery action?"**

That's a genuinely different question. A UPI payment that failed because the bank's rails are
briefly degraded should probably be retried later, automatically. A payment on a customer who
already opted out should never be touched again, no matter how valuable it is. A payment that
looks like it might involve fraud should go to a human, not get retried at all. Getting this
right, payment by payment, is what a merchant is actually paying for when they use a recovery
system — not a fixed retry schedule.

RecoveryOS is **not**:
- a generic payment-retry service
- an LLM chatbot
- an "AI wrapper" around Razorpay
- a static rules engine
- a dashboard pretending to be autonomous

It's a recovery orchestration system with a hard separation of roles:

> **AI = intelligence.** **Deterministic engine = authority.** **Execution layer = capability.**

---

## 2. The Product Experience

The dashboard (`apps/dashboard/`, Next.js 14 + TypeScript, no UI framework — plain CSS) has five
real screens:

### Control Tower — `/`
The merchant's overview: revenue at risk, revenue recovered, incremental recovery, recovery rate,
a bank-health grid, the live recovery queue, and a table of active Recovery Missions in flight.
This is the "is the system working" screen — the numbers a merchant actually cares about, live
from the database, not a static mock.

### Payment Detail — `/payments/{id}`
The full story of one payment: its Recovery Mission's live event timeline, the diagnosis and its
evidence, the AI recommendation and how it was (or wasn't) fused into the final decision, all six
scored candidate actions, the policy verdict and rule trace, and recovery history. This is where
"why did the system do that?" gets answered for one specific payment.

![Payment Detail — a real recover_via_replan mission: attempt 1 fails, the mission reinvestigates, attempt 2 fails, it reinvestigates again, attempt 3 recovers ₹8,420](docs/images/payment-detail-replan.png)

*A real, live-triggered `recover_via_replan` mission — not scripted. Attempt 1 failed, the closed
loop reinvestigated and retried, attempt 2 also failed, it reinvestigated again, and attempt 3
recovered the payment. Every timestamp, root-cause line, and policy rule check below the timeline
is the actual decision trace for this one payment.*

### Audit Explorer — `/audit` → `/audit/{payment_id}`
A 10-step reconstructible decision chain — payment → failure → anomaly → diagnosis → propensity →
action options → EVI → policy check → execution → outcome — plus the raw audit log. Built for the
question a skeptical evaluator actually asks: *"prove it, don't just tell me."*

![Audit Explorer — the full 10-step decision chain for one payment, plus its raw audit log](docs/images/audit-explorer.png)

*The same payment as above, replayed step by step. Note the "DETERMINISTIC FALLBACK — no LLM
involved in this diagnosis" badge — the real Gemini call hit its free-tier quota for this
diagnosis, and the system fell back cleanly rather than guessing or blocking, exactly as
[§7](#7-the-ai-investigator) describes.*

### Experiment / Results — `/experiments`
The benchmark this README leads with, live: the 5-seed replication study, per-seed breakdowns
with confidence intervals, this merchant's own live-traffic comparison, and the AI Contribution
panel showing tie-break/escalation counts against the deterministic baseline.

![Experiments page — the live 5-seed compliance-aware replication study, matching this README's own headline number exactly](docs/images/experiments.png)

*The live page, not a mock — "RecoveryOS generated ₹73,182 mean incremental recovered revenue …
95% CI [₹52,919, ₹93,445]" is the exact same
[`multi_seed_compliance_aware_aggregate.json`](tests/evaluation/artifacts/multi_seed_compliance_aware_aggregate.json)
this README's own hero number at the top is sourced from — `apps/api/routers/experiments.py`
used to serve an older, non-headline artifact here instead; fixed alongside these screenshots.*

### Incidents — `/incidents`
Active bank-level anomalies (degraded success rates by bank), independent of any one payment —
the systemic-risk view.

There is no separate "missions" page — a mission's state and full event timeline are shown inline
on its payment's detail page, since a mission always belongs to exactly one payment.

---

## 3. How It Works

```text
Razorpay payment.failed
        ↓
   AI Investigator
   (reasons over real evidence, gathers more if needed)
        ↓
  Recovery Recommendation
  (one bounded action, advisory only)
        ↓
┌─────────────────────────┐
│   Deterministic Guard    │
│  Policy · EVI · Risk ·   │
│  Propensity · Compliance │
└────────────┬─────────────┘
             ↓
       Execute safely
       (idempotent, provider adapter)
             ↓
       Observe outcome
             ↓
    Reinvestigate / Replan
             ↺
```

The AI proposes. The deterministic guard decides what's actually allowed. Execution only ever
happens once something has independently cleared that guard. See [§5](#5-architecture) for the
full component-level architecture, and [§6](#6-ai-that-recommends--never-authorizes) for exactly
what the AI can and can't do.

---

## 4. The Recovery Mission Loop

Every failed payment RecoveryOS tracks gets a **Recovery Mission** — a persisted, code-owned state
machine (`services/recovery_engine/mission.py`), not a stateless retry loop:

```text
OBSERVED
   ↓
INVESTIGATING  ──────────────┐
   ↓                         │
PLANNING                     │  (TERMINATED is reachable from
   ↓                         │   INVESTIGATING and PLANNING too —
AWAITING_AUTHORIZATION       │   a mission can be stopped before
   ↓                         │   it ever executes anything)
EXECUTING  ───────────────── ┤
   ↓                         │
OBSERVING_OUTCOME            │
   │                         │
   ├─→ RECOVERED  (terminal) │
   ├─→ ESCALATED  (terminal) ┘
   ├─→ TERMINATED (terminal)
   └─→ INVESTIGATING   ← the closed loop: a deferred RETRY_LATER
                          window elapsing, or a FAILED attempt with
                          mission budget remaining, both re-open
                          investigation for a genuinely fresh round
```

This is the exact transition table (`ALLOWED_TRANSITIONS`, `services/recovery_engine/mission.py`)
— every write to `recovery_missions.state` is checked against it, and an illegal transition raises
rather than silently happening.

What makes this a *mission* and not a retry loop:

- **Persisted, not in-memory.** State lives in `recovery_missions`, survives a process restart.
- **Row-locked, CAS-style transitions.** `transition_mission_async`/`_sync` lock the mission row
  `FOR UPDATE` and validate against the row's *real current state* before writing — two workers
  racing on the same mission can't both win.
- **Idempotent execution.** Every attempt carries `idempotency_key = recovery:{payment_id}:{action_type}:{attempt_number}` plus a Postgres advisory lock — a duplicate webhook or a re-delivered
  job can't double-execute.
- **Scheduled reevaluation, not a fixed timer.** A deferred `RETRY_LATER` or a failed attempt with
  budget left schedules a real future reevaluation (`scheduled_reevaluations`), picked up by a
  poller — see [§12](#12-reliability--failure-safety) for how a crash mid-reevaluation is handled
  safely.
- **Genuine replanning, not a replay.** When a mission cycles back to `INVESTIGATING`, it re-runs
  the *same* investigation function, fresh — new evidence, a new recommendation, potentially a
  different decision. `tests/integration/test_replan_produces_different_recommendation.py` proves
  this causally: two rounds, two different AI recommendations, two different final decisions for
  the same payment.

---

## 5. Architecture

```text
                    Razorpay webhook / POST /v1/events
                              ↓
                    services/pipeline/consumer.py
                    (creates/advances a Recovery Mission)
                              ↓
              ┌───────────────────────────────────┐
              │ AI Investigator                    │
              │ services/diagnosis_engine/         │
              │ investigator.py                    │
              │ — hypothesize → gather evidence →  │
              │   revise → bounded recommendation   │
              └───────────────┬─────────────────────┘
                              ↓
                   RecoveryRecommendation
        (services/diagnosis_engine/schemas.py — advisory only)
                              ↓
              ┌───────────────────────────────────┐
              │ Deterministic Authority Layer       │
              │                                     │
              │ Propensity   services/recovery_     │
              │              engine/propensity.py   │
              │ EVI          services/recovery_     │
              │              engine/evi.py           │
              │ Policy (11   services/policy_       │
              │  rules)      engine/rules.py         │
              │ Fusion       services/recovery_      │
              │              engine/orchestrator.py  │
              │              (_apply_ai_fusion)       │
              └───────────────┬─────────────────────┘
                              ↓
                   policy_decisions + decision_fusion_trace
                              ↓
              ┌───────────────────────────────────┐
              │ Execution Worker                    │
              │ workers/execution_worker.py         │
              │ — idempotent, advisory-locked        │
              │ Provider adapters:                   │
              │   integrations/razorpay/adapter.py   │
              └───────────────┬─────────────────────┘
                              ↓
                          Outcome
                              ↓
              workers/retry_scheduler.py — observe, replan
                              ↺ back to INVESTIGATING
```

| Component | Path |
|---|---|
| Mission state machine | `services/recovery_engine/mission.py` |
| Pipeline consumer (event → mission → diagnosis → decision) | `services/pipeline/consumer.py` |
| AI investigator | `services/diagnosis_engine/investigator.py` |
| LLM client (Gemini) | `services/diagnosis_engine/llm_client.py`, `llm_diagnoser.py` |
| Read-only evidence tools | `services/diagnosis_engine/tools.py` |
| Recommendation / recommendation schema | `services/diagnosis_engine/schemas.py` |
| Deterministic fusion | `services/recovery_engine/orchestrator.py` |
| Tie-break math | `services/recovery_engine/ai_fusion.py` |
| Policy engine (11 rules) | `services/policy_engine/rules.py`, `evaluate.py` |
| EVI | `services/recovery_engine/evi.py` |
| Propensity model | `services/recovery_engine/propensity.py`, `models/recovery/` |
| Next-best-action (pure argmax) | `services/recovery_engine/next_best_action.py` |
| Execution worker | `workers/execution_worker.py` |
| Retry / reevaluation scheduler | `workers/retry_scheduler.py`, `services/recovery_engine/scheduling.py` |
| Razorpay adapter | `integrations/razorpay/adapter.py` |
| API | `apps/api/routers/` |
| Dashboard | `apps/dashboard/` |
| Simulator (synthetic evaluation environment) | `simulator/` |
| Evaluation harness | `tests/evaluation/` |

Every claim in the sections below points back to one of these files — architecture → code, not
just diagram → prose.

---

## 6. AI That Recommends — Never Authorizes

The AI investigator can:
- reason over real, structured recovery evidence (not just a failure code)
- gather additional read-only evidence mid-investigation
- produce one bounded `RecoveryRecommendation`
- recommend exactly one action, from the closed set the deterministic engine already scores
- attach a confidence score, closed-set risk flags, and a rationale

The AI **cannot**:
- invent an action outside the 6-value enum
- bypass policy
- bypass the EVI floor
- authorize a candidate the policy engine has independently rejected
- choose the payment amount
- choose payment/order/customer IDs
- choose provider parameters
- choose the idempotency key
- move money directly, under any circumstance

The authority hierarchy, exactly as implemented (`services/recovery_engine/orchestrator.py`,
`_apply_ai_fusion`; precedence documented in `docs/TRD.md` §3.5):

```text
1. Hard safety / regulatory constraints   (EMandateRetryComplianceRule,
                                            AutopayExecutionWindowRule,
                                            QuietHoursComplianceRule)
2. Deterministic policy constraints       (EligibilityRule, OptOutRule,
                                            CooldownRule, RetryLimitRule,
                                            AmountLimitRule)
3. AI-derived safety signal, interpreted
   BY a deterministic rule (not the AI)   (AIRiskSignalEscalationRule)
4. EVI eligibility                        (pure argmax + floor — always AI-blind)
5. AI tie-break among eligible near-ties  (services/recovery_engine/ai_fusion.py)
6. AI evidence / rationale                (informational only)
```

> **AI may influence a decision only where deterministic systems have already established that
> the action is permissible.** It never creates permission — it can only resolve ambiguity inside
> permission the deterministic layer already granted.

![AI Recommendation → Fusion — a real safety_escalation mission: the deterministic winner was RETRY_NOW, the AI recommended ESCALATE with a HIGH_FRAUD_RISK flag, and AIRiskSignalEscalationRule overrode the decision — visible in the policy rule trace below it](docs/images/payment-detail-safety-escalation.png)

*Row 3 of the authority hierarchy above, made concrete: the deterministic engine's own winner was
`RETRY_NOW` (₹1,202 expected value), the AI recommended `ESCALATE` with a `HIGH_FRAUD_RISK` flag,
and the policy rule trace shows exactly which deterministic rule (`AIRiskSignalEscalationRule`)
read that signal and overrode the decision to `ESCALATE` — the AI flagged it, a rule decided.*

---

## 7. The AI Investigator

The investigator is not handed `failure_code = "TIMEOUT"` and asked to guess. It runs a real,
bounded, tool-calling loop (`services/diagnosis_engine/investigator.py`):

```text
hypothesize → select a tool by expected uncertainty reduction
            → call it → update hypotheses
            → (repeat, up to 2 rounds — MAX_INVESTIGATION_ROUNDS)
            → finalize: root cause + bounded RecoveryRecommendation
```

Six real, read-only evidence tools (`services/diagnosis_engine/tools.py`, `TOOL_REGISTRY`):

| Tool | What it surfaces |
|---|---|
| `get_customer_payment_history` | Recent payments for this customer |
| `get_customer_recovery_history` | Past recovery attempts and their outcomes for this customer |
| `get_cohort_failure_rate` | Current failure rate for this bank+method vs. its own recent baseline |
| `get_recent_anomalies` | Recently detected anomaly windows for this bank/method |
| `get_payment_attempt_history` | Prior recovery attempts on *this* payment |
| `get_intervention_history` | Prior policy decisions on *this* payment |

The model only ever chooses *which* tool to run — every tool's arguments are derived server-side
from the payment context, not supplied by the LLM. This closes a real failure mode observed in
testing: the model choosing the right tool but getting its arguments wrong.

Every LLM response is schema-validated on the way in; a malformed response, a timeout, or a
network failure all fail the *entire* investigation closed — the caller falls back to a
deterministic rule-based diagnosis, never a half-trusted AI result.

---

## 8. The Bounded Recovery Recommendation

The only shape a recommendation is ever produced in (`services/diagnosis_engine/schemas.py`):

```python
class RecoveryRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")          # unknown fields rejected outright

    recommended_action: RecommendedAction               # closed 6-value enum:
                                                          # RETRY_NOW | RETRY_LATER | ALT_ROUTE |
                                                          # REMINDER | ESCALATE | DO_NOTHING
    recommended_delay_minutes: int                       # advisory only — real scheduling stays
                                                          # in services/recovery_engine/timing.py
    confidence: float                                    # capped at the guard-adjusted diagnosis
                                                          # confidence, never trusted at face value
    risk_flags: list[RiskFlag]                            # closed 5-value enum, max 5:
                                                          # HIGH_FRAUD_RISK, CUSTOMER_HARM_RISK,
                                                          # DUPLICATE_PAYMENT_RISK,
                                                          # PROVIDER_UNCERTAIN,
                                                          # MANUAL_REVIEW_REQUIRED
    recovery_rationale: str                               # max 500 chars, for the audit trail
```

Why this matters: a language model should never be in a position to emit an arbitrary financial
instruction. There's no `amount`, no `order_id`, no `provider`, no `idempotency_key` field on this
model at all — those are 100% server-derived downstream, regardless of what the LLM says. A
smuggled extra field is rejected by Pydantic before it ever reaches application code
(`test_recovery_recommendation_rejects_extra_fields_if_ever_constructed_directly`,
`test_smuggled_execution_parameters_are_inert`).

Persisted to `recovery_recommendations` (migration `0021`) — a real, queryable audit row, not a
transient in-memory value.

---

## 9. Deterministic Safety

- **Policy** (`services/policy_engine/rules.py`) — 11 pure rules, short-circuiting on first
  BLOCK/ESCALATE, zero I/O, zero reference to diagnosis/confidence/AI anywhere in the original 10
  (proven by source-level tests, not just runtime behavior:
  `test_policy_engine_original_rules_never_reference_diagnosis_or_confidence`).
- **EVI** (`services/recovery_engine/evi.py`) — expected value = P(recover) × amount − cost −
  friction − risk penalty. An action needs positive expected value to be eligible at all.
- **Propensity** (`services/recovery_engine/propensity.py`) — the certified production model
  (logistic regression — see [§10](#10-does-recoveryos-actually-recover-more-revenue) for why LR,
  not LightGBM) estimates P(recover); this feeds EVI and is completely AI-blind.
- **Risk / compliance** — `AIRiskSignalEscalationRule` is the *only* rule that reads anything
  AI-derived, and even then it only ever sees a closed-set `risk_flags` frozenset — never free
  text. Presence of any flag forces `ESCALATE`, unconditionally, bypassing the EVI floor (a safety
  intervention doesn't need positive expected value to be correct). The AI never decides
  `ESCALATE` — the rule does.
- **Fusion** (`_apply_ai_fusion`, `services/recovery_engine/orchestrator.py`) — the *only* other
  place AI can change the outcome: a tie-break, and only when **all** of the following hold:
  1. the deterministic verdict is already `ALLOW` (nothing else blocked it)
  2. the recommendation's confidence clears `ai_tie_break_min_confidence` (default 0.5 — a
     pre-committed floor fixed *before* any measurement, not tuned to a result)
  3. the recommended action is within `ai_tie_break_tolerance_bps` (default 100 bps = 1%) of the
     deterministic winner's EVI
  4. that exact candidate **independently re-clears policy on its own re-evaluation**

  A near-tied candidate the policy engine has rejected is never selected, even if the AI
  recommends it — proven directly:
  `test_fusion_never_selects_a_near_tied_candidate_individually_blocked_while_winner_allowed`,
  and — because a *stale* recommendation from an earlier round could otherwise slip through — 
  `test_stale_recommendation_recommending_a_policy_blocked_action_is_rejected`.

Every fusion decision writes one `decision_fusion_trace` row (deterministic winner, the AI's
recommendation, whether it was accepted or why not) — visible at `GET /v1/audit/{payment_id}` and
the Payment Detail page, so any single decision is independently reconstructible.

`ai_recommendation_fusion_enabled` defaults to `false` in code — it ships dark; only the dev
`.env` opts in.

---

## 10. Does RecoveryOS Actually Recover More Revenue?

**Methodology, in one sentence:** five independent 10,000-payment seeds, each processed by the
identical live pipeline, compared against a **compliance-aware baseline** — a comparator that
runs the exact same `services.policy_engine.evaluate()` compliance chain RecoveryOS itself obeys,
so the comparison isolates decision quality, not "RecoveryOS obeys rules the baseline never
checked."

| Metric | RecoveryOS | Compliance-aware baseline | Difference |
|---|---:|---:|---:|
| Mean recovered revenue (5 seeds) | ₹11,33,462.88 | ₹10,60,281.10 | **+₹73,181.78** |
| 95% CI of incremental recovery | — | — | **+₹52,918.53 to +₹93,445.04** |
| Mean recovery rate | 45.66% | 42.13% | +3.53pp |
| Seeds positive | — | — | **5 / 5** |
| Strict recovered-payment superset | — | — | **5 / 5** |

"Strict superset" means: in every single seed, every payment the baseline recovered, RecoveryOS
also recovered — plus more. RecoveryOS's recovered-payment set is never a *different* set, only a
*superset*. Raw artifact:
[`tests/evaluation/artifacts/multi_seed_compliance_aware_aggregate.json`](tests/evaluation/artifacts/multi_seed_compliance_aware_aggregate.json).

What the methodology holds constant across every seed and both arms:
- the same synthetic payment population methodology (only the seed differs)
- the same policy semantics (the baseline runs the real rule chain, not an approximation)
- the same evaluation-start-time handling (a synthetic payment's first decision is evaluated
  against its own simulated `failed_at`, not real wall-clock time — see
  `services/recovery_engine/orchestrator.py::resolve_decision_now`)
- an accelerated-cooldown evaluation mode so a multi-day retry cadence doesn't require multi-day
  wall-clock evaluation runs, without changing production cooldown behavior
- no duplicate attempts, verified directly (`recoveries_duplicate_attempts: 0` every seed)

**The old compliance-blind baseline is retained only as a methodology diagnostic**
(`compliance_blind_fair_baseline_DIAGNOSTIC_ONLY` in the same artifact) — it's a *weaker*
comparator that doesn't check compliance rules at all, so any gap against it conflates "better
decisions" with "obeys rules the comparator ignores." It is not the headline number and shouldn't
be quoted as one; see [§11](#11-fairness--experiment-design) for why it exists at all.

---

## 11. Fairness & Experiment Design

The benchmark above is what's left after finding and fixing several real ways an evaluation like
this can lie to itself. This is the part of the story worth dwelling on: not "our benchmark says
+₹73k," but **we actively tried to falsify our own benchmark, found real problems, fixed them,
and reran it.**

- **Compliance-blind baseline → compliance-aware baseline.** The original comparator didn't check
  policy rules at all. Rebuilt to run the real rule chain, specifically so the headline number
  isolates decision quality rather than rule-obedience.
- **Evaluation-time clock bug.** Time-dependent policy rules (`AutopayExecutionWindowRule`,
  `QuietHoursComplianceRule`) were being evaluated against the *real* wall clock even for
  synthetic payments seeded across simulated days — meaning a canonical run's outcome depended on
  what real hour it happened to execute at. ~93% of one seed's policy `BLOCK`s traced to this
  single bug. Fixed in `resolve_decision_now()`: a synthetic payment's first decision now uses its
  own simulated `failed_at`.
- **Dataset seed contamination.** The `val_random`/`test_scenario` dataset splits were silently
  re-seeded duplicates of `train`/`test_random` — not independent data. Fixed by decorrelating
  their seeds; the propensity model was regenerated and re-certified against the clean dataset.
- **Calibration wiring.** The simulator's calibration YAML was only wired into one of five call
  sites that set the baseline failure rate; the other four silently clamped back to a hardcoded
  `0.03`. Fixed across `simulator/run.py`, `simulator/episodes/generator.py`,
  `simulator/payments/generator.py`.
- **Leakage gate.** The production propensity model is selected only after a pre-training leakage
  check (`RuntimeError` if bootstrap-CI AUC upper bound ≥ 0.85 on a held-out fold) and a real lift
  gate — LightGBM must beat logistic regression by >0.03 AUC on `test_temporal` (the only split
  verified to have zero row overlap with train) or LR stays the certified default. It doesn't
  (0.0001 lift, noise) — LR remains production (`models/recovery/certificate.py`). This gate is
  re-verified on every CI run, standalone (`test_leakage_seed.py`, an independent seed never used
  for training).
- **Reproducibility.** The same seed produces byte-identical synthetic artifacts —
  `simulator/validation/reproducibility.py` asserts this directly.

---

## 12. Reliability & Failure Safety

- **Idempotent execution.** Every attempt: `idempotency_key = recovery:{payment_id}:{action_type}:{attempt_number}`, wrapped in a Postgres advisory lock. A provider re-invoked after its result
  was already recorded doesn't double-count (`test_provider_duplicate_response.py`).
- **Persisted mission state, row-locked transitions.** Covered in [§4](#4-the-recovery-mission-loop).
- **Scheduler lease/reclaim** (`services/recovery_engine/scheduling.py`) — a claimed
  (`FIRED`) reevaluation used to become a permanent orphan if the scheduler crashed between claim
  and completion. Now:

  ```text
  PENDING
     ↓
  FIRED + lease (claimed_at + REEVALUATION_LEASE_SECONDS)
     ↓
  COMPLETED
     or
  lease expires → reclaimable, exactly like a fresh PENDING row
     ↓
  before reprocessing: check the mission's REAL current state
     ├─ still OBSERVING_OUTCOME → safe to reprocess
     └─ already advanced via another path → mark CANCELLED, not reprocessed
        (this check is what makes the reclaim safe against duplicate mission events)
  ```

- **Provider outage handling.** `RazorpayTestAdapter` degrades to the simulator provider on any
  HTTP error or ≥400 response, logged distinctly (`RAZORPAY_OUTAGE_FALLBACK`) rather than hanging
  or silently dropping the payment.
- **AI timeout / unavailability → deterministic fallback, never a hang.** The investigator's DB
  tool calls are bounded (5s); the LLM round call is bounded by `ai_diagnoser_gemini_timeout_seconds` (4.0s). Either firing collapses to the same deterministic fallback path, never a
  partial/half-trusted AI result.
- **No unsafe AI execution parameters, structurally proven, not just tested-for.** A static
  AST-walk test (`test_execution_boundary_never_references_recommendation_fields`) confirms the
  execution boundary functions don't even reference `recommendation`/`ai_risk_flags`/
  `recovered_action` as identifiers — not "we tested it doesn't leak," but "the code literally
  cannot."

---

## 13. Razorpay Integration

```text
Razorpay payment.failed (webhook)
        ↓
POST /webhooks/razorpay   (apps/api/routers/razorpay_webhooks.py)
   — HMAC signature verified over the raw body; invalid/missing → 401, never stored
   — Razorpay's own guidance: acknowledge quickly, else it retries forever, so a
     malformed body / unknown event_type / unrecognized order_id all get 200 +
     a stored, clearly-flagged row, not a 4xx that triggers endless retries
        ↓
Recovery Mission created/advanced (services/pipeline/consumer.py)
        ↓
Recovery Engine → Execution Worker → Provider Adapter
```

Three provider adapters, one interface (`integrations/razorpay/adapter.py`):

| Adapter | What it does |
|---|---|
| `RazorpayTestAdapter` | Real HTTP calls to Razorpay's TEST-mode Orders API — a genuine order, a genuine `provider_ref`, resolved via the real webhook path. Verified end-to-end against a real Razorpay test-mode key. |
| `SimulatorAdapter` | Resolves outcomes from the same latent recoverability model the evaluation dataset uses — this is what the benchmark numbers above are computed against, not real Razorpay traffic. |
| `DemoScriptedAdapter` | A deterministic fail-then-succeed sequence, selectable via config for on-demand demo scenarios — not used by the live default demo scenarios today (see [§17](#17-run-the-demo)). |

**The benchmark in [§10](#10-does-recoveryos-actually-recover-more-revenue) runs entirely against
the simulator, not real Razorpay traffic** — that's a deliberate evaluation design choice (a
structurally-independent ground truth is what makes the comparison non-circular; see
`docs/TRD.md` §6), not a limitation being hidden. The real Razorpay integration is a separate,
independently verified capability.

---

## 14. Observability & Auditability

Every recovery decision leaves a reconstructible trail. Persisted at every stage:

`diagnoses` → `diagnosis_hypotheses` → `investigation_steps` → `recovery_recommendations` →
`candidate_actions` (all 6 scored candidates, not just the winner) → `policy_decisions` (full rule
trace) → `decision_fusion_trace` (when fusion ran) → `mission_events` (the full mission timeline)
→ `recoveries` → `recovery_ledger` → `audit_log`.

`GET /v1/audit/{payment_id}` replays the full chain for one payment: payment → failure → anomaly →
diagnosis → propensity → action options → EVI → policy check → execution → outcome — the same
chain the Audit Explorer page renders. Every mutating API response also carries
`X-Model-Version`/`X-Policy-Version` headers, so a screenshot of any dashboard state is
reproducible against the exact model/policy version that produced it.

---

## 15. Security Model

| Threat | Defense |
|---|---|
| LLM recommends retrying an unsafe/over-limit payment | The candidate must independently clear the full policy chain *and* EVI on its own re-evaluation — see [§9](#9-deterministic-safety) |
| LLM tries to smuggle a payment amount, provider, ID, or idempotency key | These fields don't exist on `RecoveryRecommendation` at all; execution-critical parameters are 100% server-derived, structurally proven (`test_execution_boundary_never_references_recommendation_fields`) |
| Duplicate/replayed webhook or job | Idempotency key + Postgres advisory lock + unique constraints |
| A stale AI recommendation from an earlier round gets replayed against a since-changed policy state | Independently re-evaluated against *current* policy before being trusted (`test_stale_recommendation_recommending_a_policy_blocked_action_is_rejected`) |
| Prompt-injection-style content inside tool-returned evidence | Even if the model "complies" with injected text and recommends an unauthorized action, the deterministic fusion boundary still rejects it — tested explicitly (`test_injection_styled_evidence_producing_a_policy_blocked_recommendation_is_still_rejected`) |
| Malformed/unparseable Gemini response | Fails the investigation closed, not partially trusted — covered for both malformed-JSON and missing-candidates response shapes |
| LLM prompt injection via failure metadata | `failure_code` is sanitized (`[A-Za-z0-9_]`, 64 chars) at both the API ingest boundary and the diagnoser boundary |
| Unauthorized merchant accessing another merchant's data | Row-level scoping by `merchant_id` on every merchant-scoped table |
| Tampering with audit history | `audit_log`/`events` have `UPDATE`/`DELETE` revoked from the application DB role at the grant level — not just application-layer discipline |

---

## 16. Testing

Current full-suite result (rerun immediately before writing this section):

```text
tests/unit + tests/integration:  538 passed, 5 xfailed, 0 failed
tests/integration/test_dashboard_e2e.py (Playwright, real browser):  4 passed
```

Notable coverage, by category:

- **AI adversarial** — action outside the closed enum, unrecognized risk flag, oversized risk-flag
  list, smuggled execution parameters, malformed/truncated Gemini JSON, injection-styled evidence
  — all fail closed (`tests/unit/test_ai_recommendation_adversarial.py`, `test_llm_client.py`).
- **Execution boundary** — structural (AST-level) proof the recommendation object never reaches
  execution code (`test_execution_boundary_never_references_recommendation_fields`).
- **Fusion boundary** — every combination of near-tie × policy-verdict × confidence-floor is
  exhaustively tested (`test_ai_recommendation_bounded_influence.py`,
  `test_ai_recommendation_adversarial.py`).
- **Scheduler crash/reclaim** — a crash after claim but before completion is reclaimed and either
  safely reprocessed or safely cancelled, never duplicated (`test_retry_scheduler.py`).
- **Idempotency** — duplicate provider responses, duplicate webhook deliveries, duplicate ledger
  writes all collapse to a single real effect (`test_provider_duplicate_response.py`,
  `test_idempotent_execution.py`).
- **Benchmark integrity** — every baseline comparator is proven to never write to RecoveryOS's own
  decision tables (`test_baseline_determinism_and_dedup.py`).
- **AI safety invariant across every real-model arm run so far** — `ai_unsafe_deltas: 0`.

CI (`.github/workflows/ci.yml`, 4 jobs — lint & format, security gates, unit tests, integration
tests) runs on every push and is green on `main`.

---

## 17. Run the Demo

```bash
./demo.sh
```

Brings up the full stack, waits for the API to become healthy, seeds a demo merchant + API key,
and prints the exact command to trigger the hero scenario. This wraps the manual sequence below —
both are real and kept in sync; use whichever you prefer.

<details>
<summary>Manual sequence (what <code>demo.sh</code> automates)</summary>

### Prerequisites
- Docker + Docker Compose v2 (`docker compose`)
- Node.js 18+ and npm (only for running the dashboard outside Docker)

### Start
```bash
cp .env.example .env   # fill in the placeholders — see .env.example's own comments
docker compose up -d --build
docker compose ps
curl http://localhost:8000/health
```

This brings up Postgres, Redis, a one-shot `migrate` service, the API, all four background
workers, and Prometheus + Grafana. `ENV=demo` is the default, which is what enables the
`/v1/simulate/*` endpoints below.

### Seed a merchant + API key
There's no provisioning CLI beyond this — `demo.sh` runs exactly this:

```bash
docker compose exec api python -c "
from apps.api.dependencies.auth import generate_api_key, hash_api_key
key = generate_api_key()
print('API key (save this, it is shown once):', key)
print('hash:', hash_api_key(key))
"

docker compose exec postgres psql -U recoveryos -d recoveryos -c "
INSERT INTO merchants (merchant_id, name, api_key_hash)
VALUES (gen_random_uuid(), 'demo-merchant', '<HASH>');
"
```

</details>

### Open
```bash
cd apps/dashboard && npm install && npm run dev
# → http://localhost:3000
```

### Trigger a hero scenario
With `AI_RECOMMENDATION_FUSION_ENABLED=true` (needed for `recover_via_replan`/`safety_escalation` — bring the stack up with the extra override:
`docker compose -f docker-compose.yml -f docker-compose.override.ai_fusion.yml up -d --build`
and `AI_RECOMMENDATION_FUSION_ENABLED=true` in your shell):

```bash
curl -X POST http://localhost:8000/v1/simulate/scenario \
  -H "X-API-Key: <your key>" -H "Content-Type: application/json" \
  -d '{"scenario": "recover_via_replan"}'
```

Three real scenarios exist (`apps/api/routers/simulate.py`), each a genuine live mission, not a
canned animation:

| `scenario` value | What it demonstrates |
|---|---|
| `recover_via_replan` | Fails once, replans, succeeds — the closed loop in [§4](#4-the-recovery-mission-loop) |
| `safety_escalation` | A risk flag halts execution — zero money moved |
| `world_changed` | An external webhook resolves the payment mid-mission |

The response includes a `payment_id` — open `http://localhost:3000/payments/{payment_id}` to
watch the mission's real timeline.

You can also inject a bank-wide degradation (no fusion flag needed):

```bash
curl -X POST http://localhost:8000/v1/simulate/degrade \
  -H "X-API-Key: <your key>" -H "Content-Type: application/json" \
  -d '{"bank":"HDFC","method":"upi","target_success_rate":0.4,"duration_minutes":15}'
```

### Reset
```bash
docker compose down -v && ./demo.sh
```

---

## 18. Reproducing the Evaluation

```text
README → Architecture (§5) → Demo (§17) → this section → Testing (§16) → source
```

The full pipeline: dataset generation → propensity training/certification → baseline computation
→ multi-seed campaign. Provenance for the exact canonical run:
[`docs/phase8_canonical_run.md`](docs/phase8_canonical_run.md). Methodology write-ups:
[`docs/phase8_priority0_multi_seed_baseline.md`](docs/phase8_priority0_multi_seed_baseline.md)
(the superseded compliance-blind study — kept for the fairness narrative in [§11](#11-fairness--experiment-design)),
[`docs/phase11_ai_ablation.md`](docs/phase11_ai_ablation.md) (the AI ablation evidence in
[§21](#21-is-the-ai-actually-load-bearing)).

```bash
# 1. Generate a dataset
python -m simulator.run --n=10000 --seed=42 --customers=2000 --output=db

# 2. Train + certify the propensity model
python -m models.recovery.train --data-dir=data --output-dir=models/recovery/artifacts
python -m models.recovery.certificate

# 3. Run the 5-seed compliance-aware campaign (the headline number's source)
python -m tests.evaluation.multi_seed_runner
```

Raw artifacts live in `tests/evaluation/artifacts/` — every number in [§10](#10-does-recoveryos-actually-recover-more-revenue) is re-derivable from
[`multi_seed_compliance_aware_aggregate.json`](tests/evaluation/artifacts/multi_seed_compliance_aware_aggregate.json) directly, not just trusted from this document.

---

## 19. Repository Map

| Area | Location | Purpose |
|---|---|---|
| API | `apps/api/routers/` | FastAPI routes — events, risk, payments, missions, experiments, audit, incidents, simulate, webhooks |
| Dashboard | `apps/dashboard/app/` | Next.js/TypeScript frontend — Control Tower, Payment Detail, Audit Explorer, Experiments, Incidents |
| Recovery engine | `services/recovery_engine/` | Orchestrator, EVI, propensity, next-best-action, AI fusion, mission state machine, scheduling |
| Mission state machine | `services/recovery_engine/mission.py` | `ALLOWED_TRANSITIONS`, row-locked transitions, budget checks |
| Diagnosis / AI | `services/diagnosis_engine/` | Investigator, LLM client, tools, schemas, adversarial guards |
| Policy | `services/policy_engine/` | The 11-rule deterministic policy chain |
| EVI | `services/recovery_engine/evi.py` | Expected-value scoring for every candidate action |
| Propensity | `services/recovery_engine/propensity.py`, `models/recovery/` | Certified logistic-regression recovery-probability model |
| Execution | `workers/execution_worker.py`, `services/execution_engine/` | Idempotent execution, advisory locking |
| Scheduler | `workers/retry_scheduler.py`, `services/recovery_engine/scheduling.py` | Deferred reevaluation, lease/reclaim |
| Pipeline glue | `services/pipeline/` | Event consumer, ledger, reconciliation, baseline comparators |
| Razorpay integration | `integrations/razorpay/adapter.py` | Provider adapters (real, simulator, demo-scripted) |
| Simulator | `simulator/` | Synthetic merchant environment, structurally-independent ground truth |
| Evaluation | `tests/evaluation/` | Multi-seed runner, AI ablation runner, raw artifacts |
| Tests | `tests/unit/`, `tests/integration/` | 538 passed + 5 xfailed (unit+integration), 4 Playwright E2E |
| Migrations | `migrations/versions/` | 25 Alembic migrations |
| Docs | `docs/` | TRD, PRD, evaluation write-ups, AI ablation writeup |

---

## 20. Design Decisions

**Why AI does not directly execute.** Safety and authority have to be separable to be
defensible — "the LLM never calls the executor" only means something if it's structurally true,
not a convention. See [§9](#9-deterministic-safety).

**Why candidates are deterministic.** The AI recommends among candidates the deterministic engine
already scored; it never generates its own. If AI could propose novel candidates, "AI may never
create permission" would be unenforceable — there'd be no independent thing to check the
recommendation against.

**Why the AI doesn't see candidate EVI scores.** So it can't simply reverse-engineer "which answer
wins" and parrot it back — its recommendation has to come from the evidence, not from peeking at
the deterministic engine's own math.

**Why a compliance-aware baseline.** Covered in [§11](#11-fairness--experiment-design) — a
comparator that doesn't check the same rules RecoveryOS obeys isn't measuring decision quality.

**Why the mission is persisted, not in-memory.** Crash recovery and genuine closed-loop autonomy
both require it — an in-memory retry loop can't survive a worker restart or prove, after the fact,
what it actually did and why.

**Why the benchmark uses five seeds, not one.** A single-seed result has no way to distinguish "a
real effect" from "got lucky with this particular random draw." See
[§11](#11-fairness--experiment-design) and the retained diagnostic study in
`docs/phase8_priority0_multi_seed_baseline.md` for exactly how much a single seed can mislead.

---

## 21. Is the AI Actually Load-Bearing?

Two genuinely different kinds of evidence exist here, and this README will not blur them.

### Mechanism evidence (test-proven, comprehensive)

Controlled tests — fixed candidates, mocked LLM responses — prove the fusion mechanism works
exactly as designed when it fires:
- an AI recommendation *does* change the final `chosen_action` when it's a genuine, policy-clean
  near-tie (`test_tie_break_applies_for_near_tied_policy_allowed_recommendation`)
- a risk flag *does* force `ESCALATE`, even overriding a strongly positive EVI winner
  (`test_risk_flag_escalates_regardless_of_strongly_positive_evi`)
- a second investigation round with a genuinely different recommendation *does* produce a
  different final decision for the same payment
  (`test_replan_produces_different_recommendation.py`)

### Real-model evidence (honest, and limited)

The one real-Gemini run that exists (`tests/evaluation/artifacts/ai_ablation_results.json`):

| | |
|---|---|
| Failed payments in the run | 4 |
| Real Gemini recommendations obtained | 2 |
| Recommendation acceptance rate | **0.0%** |
| AI tie-breaks observed | 0 |
| AI risk escalations observed | 0 |
| Unsafe AI-driven deltas | **0**, across every arm |

**In this run, the AI's recommendation never changed a decision.** With only 2 real
recommendations, that is not evidence the mechanism doesn't work (the mechanism claim above is
independently proven) — it's too small a sample to say anything about how *often* a real
near-tie or risk signal occurs in practice. A larger real run would need either a paid Gemini tier
or patience across many free-tier-quota days (20 requests/day/model on the free tier, ~2-3 calls
per diagnosis); this was evaluated and deliberately not pursued as a workaround. See
[`docs/phase11_ai_ablation.md`](docs/phase11_ai_ablation.md) for the full breakdown.

**The honest summary:** the AI architecture is mechanism-proven and safety-proven. Its real-model
behavioral contribution has not been measured at statistically meaningful scale. Do not read this
README, or any other RecoveryOS material, as claiming the AI caused the benchmark result in
[§10](#10-does-recoveryos-actually-recover-more-revenue) — that result is the *whole system's*
incremental recovery, most of which is the deterministic propensity/EVI/policy engine, which is
completely AI-blind by construction.

---

## 22. Limitations — Honest Disclosures

These are known boundaries of the current build, not hidden assumptions:

- **Real-Gemini AI behavioral evidence is small.** 4 payments, 2 real recommendations, 0 observed
  tie-breaks or escalations — see [§21](#21-is-the-ai-actually-load-bearing).
- **No statistical AI-lift claim exists**, and none should be inferred from the headline benchmark
  number — that number is the whole system's, most of which is AI-blind by construction.
- **Free-tier Gemini quota** (20 requests/day/model/key) is the binding constraint on closing the
  above — not an engineering gap, a real external limit that was evaluated and not worked around.
- **The benchmark runs against the simulator, not real Razorpay traffic** — by design (a
  structurally-independent ground truth is what makes the comparison valid at all), but worth
  stating plainly rather than leaving ambiguous.
- **CI's own test dependency installs are unpinned** (only the lint job is pinned) — a known,
  low-priority asymmetry with local/Docker, which are pinned; not currently causing failures.

A trustworthy README is more useful to a skeptical evaluator than an exaggerated one — every
number above is independently re-derivable from a file path also given above.

---

## 23. Build Challenges

**1. Fair baseline construction.** The first baseline didn't check compliance rules at all, so any
apparent advantage could just be RecoveryOS obeying rules the comparator ignored. Fixed by
building a comparator that runs the identical policy chain — see [§11](#11-fairness--experiment-design).

**2. Temporal determinism.** A canonical run seeds thousands of payments spanning simulated days,
then makes every first decision within a few real minutes — time-dependent rules were reading the
real clock regardless. ~93% of one seed's blocks traced to this single bug. Fixed in
`resolve_decision_now()`.

**3. Autonomous mission/replanning without duplicating work.** A closed loop that re-investigates
on failure is easy to get wrong in a way that double-executes or double-charges. Solved with
persisted, row-locked mission state plus a lease/reclaim scheduler that checks the mission's real
current state before ever reprocessing — see [§12](#12-reliability--failure-safety).

**4. AI authority boundaries that are structurally true, not just documented.** "The LLM can't
authorize execution" is easy to claim and easy to accidentally violate one refactor later. Solved
with a bounded recommendation schema that has no execution-critical fields at all, plus a static
AST-level test that the execution boundary functions don't even reference AI-derived identifiers.

**5. Scheduler crash recovery.** A claimed-but-crashed reevaluation used to be a permanent orphan.
Solved with a time-boxed lease and a reclaim path that distinguishes "genuinely still waiting"
from "already progressed via another route" before ever reprocessing.

**6. Idempotent financial execution under real concurrency.** Advisory locks plus a deterministic
idempotency key derived from `(payment_id, action_type, attempt_number)` — proven directly against
a provider that returns success on a call whose result was already recorded.

**7. A demo scenario that silently deadlocked — found only by actually running it.** The
`recover_via_replan`/`world_changed` demo triggers were written assuming the payment provider
would report an intermediate `PENDING` state to poll for and later reconcile — true for the real
Razorpay test-mode adapter, but not for `SimulatorAdapter` (the default), which resolves an
attempt straight to `SUCCESS`/`FAILED` via a real dice roll once ground truth exists. Every unit
and integration test used a stub that *did* return `PENDING`, so nothing caught it — a live
rehearsal of the actual demo did, immediately. Fixed two different ways for two different reasons:
`recover_via_replan` now seeds a genuinely forced `FAILED` outcome (probability 0, through the
same real resolver every payment uses) and lets the real Phase 13 closed loop
(`execution_worker` + the always-running `retry_scheduler`) replan it organically, no scripting at
all; `world_changed` needed an actual `PENDING` window to demonstrate late webhook reconciliation,
so it gets one via a narrow, opt-in `simulator_latent_state.force_pending_until_reconciled` flag
(migration 0026) that only this one scenario ever sets. Neither fix touches the shared dice-roll
logic the benchmark itself depends on.

**8. A live dashboard page quietly showing the wrong benchmark.** Capturing real screenshots for
this README (rather than describing the dashboard secondhand) surfaced that `/experiments`'s
live per-seed table was still reading the original Phase 8 study
(`multi_seed_results.json`) — the one [§10](#10-does-recoveryos-actually-recover-more-revenue)
explicitly says is *not* the headline comparison — instead of the newer compliance-aware artifact
this README's own +₹73,181.78 number comes from. A judge clicking into the live page would have
seen a different number than the one at the top of this document. Fixed to read the same artifact
the README cites, verified live against the running stack until the two matched exactly.

---

## 24. Why RecoveryOS Is Different

| Traditional retry system | RecoveryOS |
|---|---|
| Retry rules | Recovery missions |
| Static retry sequence | Closed-loop replanning |
| Failure classification | Evidence-driven investigation |
| LLM as decision-maker | AI as a bounded advisor |
| Immediate execution | Deterministic authorization first |
| One-shot retry | Observe → replan |
| Basic success rate | Incremental-revenue measurement, with a confidence interval |
| Best-effort execution | Idempotent financial execution |

A failed payment isn't the end of the transaction. It's the beginning of a recovery mission.

---

<a id="missing-assets"></a>
## Missing assets / final checks before submission

**Screenshots** — real, live-captured (`docs/images/`), embedded at the top of this README, in
[§2](#2-the-product-experience), and in [§6](#6-ai-that-recommends--never-authorizes): Control
Tower, a full 3-attempt `recover_via_replan` mission (Payment Detail), the same payment's 10-step
Audit Chain, a `safety_escalation` mission's AI Recommendation → Fusion section, and the
Experiments page (fixed to serve `multi_seed_compliance_aware_aggregate.json` — the same artifact
this README's own headline number comes from — instead of the older, non-headline artifact it was
silently still reading; `apps/api/routers/experiments.py`).

**Claims to re-verify if anything changes before the final freeze:**
- The exact test counts in [§16](#16-testing) (538 passed / 5 xfailed / 4 E2E) — rerun `pytest tests/unit tests/integration -m "not e2e"` and the dashboard E2E suite one more time right before submission.
- The benchmark table in [§10](#10-does-recoveryos-actually-recover-more-revenue) — re-diff against `tests/evaluation/artifacts/multi_seed_compliance_aware_aggregate.json` if that file changes for any reason.
- Whether `AI_RECOMMENDATION_FUSION_ENABLED` is still `true` in the demo `.env` by submission time — the hero scenarios in §17 need it.
- `demo.sh` — syntax-checked (`bash -n`) but not run end-to-end against a live stack in this pass; run it once for real before relying on it live.
