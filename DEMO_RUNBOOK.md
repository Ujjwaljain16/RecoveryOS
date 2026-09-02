# RecoveryOS — Final Demo Runbook

**Status: corrected against real, live-verified behavior.** The original version of this document
was a planning document derived purely from reading code, before the demo scenario engine had
actually been rehearsed live. Live rehearsal surfaced two real bugs in
`apps/api/routers/simulate.py` (both fixed, both verified against the actual running stack) that
changed how `recover_via_replan` and `world_changed` actually work — this revision corrects every
passage below that described the pre-fix mechanism. See the git history for
`apps/api/routers/simulate.py` and `integrations/razorpay/adapter.py` for the exact fixes.

---

## 0. The single most important finding

`POST /v1/simulate/scenario` is not a canned demo — it's a real scenario **engine**, already built
(`apps/api/routers/simulate.py`), and it changes the whole plan:

- **`recover_via_replan`** and **`world_changed`** run the payment through the **real, live**
  pipeline — real `diagnose_and_persist` (the real AI investigator, including a real Gemini call
  if `GEMINI_API_KEY` is configured, which it is in the dev `.env`), real EVI/policy, real
  enqueue. The two scenarios now get their determinism through **different** mechanisms, because
  `SimulatorAdapter` (the default provider) resolves an attempt directly to SUCCESS/FAILED via a
  real dice roll — it never produces an intermediate PENDING state on its own:
  - **`recover_via_replan`** seeds attempt 1's own ground-truth recovery probability at 0 —
    a real, deterministic loss through the SAME dice-roll function every other payment in this
    system resolves through, not a special case. From there, nothing in `simulate.py` drives
    anything further: the real Phase 13 closed loop (`workers/execution_worker.py`'s
    `_advance_mission_after_outcome` scheduling a real re-evaluation, picked up organically by the
    always-running `retry_scheduler` container within ~5s) does the rest, exactly like a genuine
    production failure would. Attempt 2 (and, if it also organically fails, attempt 3) is a real,
    unforced dice roll — very likely to succeed given the seeded latents, but not guaranteed. A
    live rehearsal run actually took 3 attempts, not 2 — see §4.
  - **`world_changed`** instead seeds an opt-in flag
    (`simulator_latent_state.force_pending_until_reconciled`, migration 0026) that makes
    `SimulatorAdapter` skip its dice roll and return a real PENDING outcome — giving this one
    scenario a genuine PENDING window, which `simulate.py`'s own background task then resolves via
    the exact same `reconcile_pending_recovery` path a real webhook would use, ~4s later ("the
    world changes").
  - Neither mechanism is a fake UI transition — both route through real, unmodified production
    code; they differ only in *how* each scenario gets a demo-deterministic starting point.
- **`safety_escalation`** is different on purpose: the diagnosis/recommendation content is
  pre-scripted (`risk_flags=["HIGH_FRAUD_RISK"]`), specifically for reliability — but everything
  *after* that (`AIRiskSignalEscalationRule`, the fusion boundary, the mission transition to
  `ESCALATED`) is the real code path, unmodified. It's also fully synchronous — it resolves
  completely before the HTTP response returns. Zero timing risk, zero Gemini quota cost.
- Both real-pipeline scenarios require `AI_RECOMMENDATION_FUSION_ENABLED=true` — the endpoint
  409s otherwise rather than silently producing a misleading result. This needs
  `docker-compose.override.ai_fusion.yml`.
- **Real Gemini quota is genuinely consumed by `recover_via_replan`/`world_changed`** — each
  round of real investigation costs up to 2-3 Gemini calls (`MAX_INVESTIGATION_ROUNDS=2` +
  1 finalize call). `world_changed` runs ONE round. `recover_via_replan` runs at least TWO full
  rounds (initial + one guaranteed replan), and POSSIBLY A THIRD if attempt 2 also organically
  fails (the demo policy config allows up to 3 attempts) — budget for the worst case, not just the
  common case. See §13.
- The hero payment is real and specific: **₹8,420** (`842_000` paise, `_seed_scenario_payment`'s
  default), card payment, a randomly-suffixed `DEMO_BANK_xxxxxxxx` bank. Whichever attempt
  `recover_via_replan`/`world_changed` finally succeeds on recovers exactly this amount.
- The mission timeline UI auto-polls every 3 seconds until a terminal state
  (`app/payments/[id]/page.tsx`) — no manual refresh needed during recording.

---

## 1. Executive demo concept

One story, told once, real: **a payment fails, RecoveryOS investigates it for real, a deterministic
guard decides what's allowed, the first attempt fails, the system notices and replans without
being told to, and it eventually recovers the payment (attempt 2, usually — occasionally attempt
3) — and separately, the benchmark proves this pattern creates positive incremental revenue across
5 independent seeds, honestly compared against a baseline that obeys the same rules.**

The AI story is told carefully: real investigation is shown live (`recover_via_replan`), and the
"AI can't authorize money movement even when it flags something serious" story is told through a
scenario built for 100% reliability (`safety_escalation`) — clearly distinguished from the
benchmark, never conflated with it.

---

## 2. 5-minute timeline

| Time | Section | Scenario used |
|---|---|---|
| 0:00–0:30 | The hook | — |
| 0:30–1:15 | Product model (Control Tower) | — |
| 1:15–2:30 | The hero recovery mission | `recover_via_replan` |
| 2:30–3:20 | AI + safety | `safety_escalation` (+ referencing the fusion trace from the hero mission) |
| 3:20–4:05 | Proof this isn't a retry system | (same hero mission, replayed via the timeline already on screen) |
| 4:05–4:35 | Business proof / benchmark | Experiments page |
| 4:35–4:55 | Engineering credibility | Architecture/audit view |
| 4:55–5:00 | Close | Control Tower |

---

## 3. Exact navigation path

1. Browser already open at `http://localhost:3000` (Control Tower), API key already configured
   in the dashboard's proxy (server-side, never visible on screen).
2. **[0:00]** Start on Control Tower, camera on the page as-is.
3. **[0:30]** Point at 3 cards: Revenue at Risk, Recovered, Active Recovery Missions table (empty
   or near-empty at this point — that's fine, it primes what's about to happen).
4. **[~0:50]** Trigger `recover_via_replan` (pre-run via terminal or a demo-trigger button — see
   §6 on which). Response includes `payment_id`.
5. **[1:15]** Navigate to `http://localhost:3000/payments/{payment_id}`.
6. Let the Recovery Mission timeline visibly populate in real time (auto-polling, no refresh) —
   real, live-verified event sequence (`mission_events.event_type`, actor in parens):
   `MISSION_CREATED` (system) → `HYPOTHESIS_UPDATED` (ai) → `INVESTIGATION_CONCLUDED` (system) →
   `PLANNING_CONCLUDED` (system) → `POLICY_AUTHORIZED` (policy_engine) → `RECOVERY_FAILED`
   (execution_worker) → `REINVESTIGATION_STARTED` (system) → [same
   HYPOTHESIS_UPDATED/INVESTIGATION_CONCLUDED/PLANNING_CONCLUDED/POLICY_AUTHORIZED block again] →
   `RECOVERY_SUCCEEDED` (execution_worker) → `MISSION_RECOVERED` (system). If attempt 2 also
   organically fails, the RECOVERY_FAILED/REINVESTIGATION_STARTED block repeats a second time
   before the final RECOVERY_SUCCEEDED — don't be surprised by a 3-round timeline, see §4.
7. **[2:30]** Scroll to the "AI Recommendation → Fusion" section on this same page — show the
   recommendation, confidence, and fusion reason for one of the two rounds.
8. Cut to a **second, pre-triggered** `safety_escalation` mission's Payment Detail page (a
   different `payment_id`, triggered earlier so it's already resolved) — show `AI_RECOMMENDATION`
   ("Recommends ESCALATE") immediately followed by `POLICY_ESCALATED`, and the fact that
   `recoveries` has zero rows for this payment (zero money moved).
9. **[3:20]** Return to the hero mission's timeline, scroll through the full event list top to
   bottom in one continuous motion — this is the "closed loop, not a retry" beat.
10. **[4:05]** Navigate to `http://localhost:3000/experiments`.
11. Show the 5-seed table and the headline incremental-recovery number.
12. **[4:35]** Navigate to `http://localhost:3000/audit/{payment_id}` for the hero payment — the
    10-step chain, as the "engineering credibility" beat.
13. **[4:55]** Return to Control Tower — now showing the hero mission as `RECOVERED` in the
    Active Missions table (or moved out if terminal missions drop off that list — verify during
    rehearsal, see §8).

---

## 4. Hero scenario

**`recover_via_replan`**, exactly as implemented, no modification:

- ₹8,420 card payment, `failure_code=TIMEOUT`, `failure_class=TEMPORARY`, on a fresh
  `DEMO_BANK_xxxxxxxx`.
- Round 1: real investigation, real (or fallback, if Gemini is unavailable at that moment) AI
  recommendation, real EVI/policy — under the demo's action-cost configuration (`RETRY_NOW` cheap,
  everything else deliberately uneconomical), `RETRY_NOW` is the real, decisive deterministic
  winner every time. **Say this honestly on camera**: this proves the investigation and
  recommendation pipeline is genuinely live, not that the AI changed which action won — with
  costs skewed this far apart, no near-tie is possible. That's a deliberate demo-reliability
  choice, not something to hide (see §6).
- Attempt 1 is a real, deterministic loss: `_seed_scenario_payment` seeds this payment's ground-
  truth recovery probability at exactly 0, so the SAME dice-roll function every payment in this
  system resolves through genuinely returns FAILED — not scripted around, actually forced through
  the real mechanism.
- That real FAILED outcome hits `workers/execution_worker.py`'s own real Phase 13 code
  (`_advance_mission_after_outcome`), which schedules a real re-evaluation — due immediately,
  since the demo policy config sets `retry_cooldown_hours=0`. Nothing in `simulate.py` invokes
  anything further: the **always-running `retry_scheduler` container** picks this up on its own
  next poll cycle (`POLL_INTERVAL_SECONDS=5`) and runs a genuinely fresh second investigation
  round — organically, exactly like production.
- Attempt 2 is a real, unforced dice roll against this payment's seeded latents (patience 0.8,
  bank health 0.9, `TEMPORARY_GATEWAY_TIMEOUT`) — very likely to succeed, but not guaranteed. If it
  succeeds: ₹8,420 recovered, mission `RECOVERED`, two rounds total. **If it also fails** (real,
  live-observed behavior, not a hypothetical): the same real closed loop fires again, producing a
  genuine third investigation round and attempt — the demo policy config allows up to 3 attempts,
  so this still resolves, just one round later and a few seconds slower. Either ending is a real,
  honest outcome; don't be thrown by a 3-round timeline on camera, and don't narrate a fixed
  "attempt 2 succeeds" script that could be contradicted by what's actually on screen — narrate
  what's actually happening (see §10's phrasing, which already avoids committing to attempt 2
  specifically).

**Why this is the right hero scenario**, not a fabricated one: every step routes through the same
functions production traffic uses (`services/pipeline/consumer.py`,
`services/recovery_engine/orchestrator.py`, `workers/execution_worker.py`,
`workers/retry_scheduler.py`) — the only scripted part is forcing attempt 1's own dice roll to
land on FAILED, which is exactly what a demo needs to be deterministic and repeatable without
becoming fake; everything downstream of that single seeded value is completely real and unforced.

---

## 5. Screen-by-screen storyboard

### Screen 1 — Control Tower (`/`)
- **Action:** none yet, just visible.
- **Visible state:** Revenue at Risk, Recovered, Incremental Recovery, Recovery Rate cards; bank
  health grid; Recovery Queue; Active Recovery Missions table.
- **Backend event:** none — this is a live read of current DB state.
- **Technical significance:** proves the dashboard is a live operational view, not a static mock.
- **Business significance:** this is what a merchant actually watches day to day.
- **Narration:** *"This is the Control Tower — every number here is live from the database, not a
  demo animation."*
- **Duration:** ~15s.
- **Failure recovery:** if cards show stale numbers from a prior test, that's a clean-room prep
  failure, not a recording-time fix — see §8.

### Screen 2 — Payment Detail, hero mission, early state (`/payments/{id}`)
- **Action:** open right after triggering `recover_via_replan`.
- **Visible state:** mission badge shows `INVESTIGATING`/`PLANNING`, timeline starts populating.
- **Backend event:** `process_payment_failure` running for real — diagnosis, recommendation,
  policy decision, enqueue.
- **Technical significance:** a real investigation is happening, not a pre-recorded animation.
- **Business significance:** "the system already started working on this before I even opened
  the page."
- **Narration:** *"The moment this payment failed, RecoveryOS opened a Recovery Mission and
  started investigating why."*
- **Duration:** ~20s (investigation + first decision typically lands within this window).
- **Failure recovery:** if nothing appears within ~15s, the polling interval is 3s — wait one more
  cycle before assuming something's wrong; see §9.

### Screen 3 — Payment Detail, mid-mission (attempt 1 fails, replan)
- **Action:** stay on the same page, let it auto-update.
- **Visible state:** `RECOVERY_FAILED` appears, then `REINVESTIGATION_STARTED`
  ("Previous attempt failed — reinvestigating with new evidence"), then a fresh
  `HYPOTHESIS_UPDATED`/`AI_RECOMMENDATION` pair.
- **Backend event:** a real FAILED outcome (attempt 1's ground-truth probability was seeded at 0)
  schedules a real re-evaluation, picked up by the always-running `retry_scheduler` container's
  own next poll cycle (within ~5s) → a genuinely fresh second investigation round.
- **Technical significance:** this is the closed loop — a second, independent decision, not a
  scripted retry count.
- **Business significance:** "it didn't just try the same thing twice — it reconsidered."
- **Narration:** *"The first attempt failed. RecoveryOS noticed, gathered fresh evidence, and
  replanned — automatically."*
- **Duration:** ~10-15s (the scheduler's own poll interval, not a webhook wait).
- **Failure recovery:** if the replan doesn't fire within ~20s, check
  `docker compose logs retry_scheduler` — confirm the container is actually running before
  assuming a real bug (see §14 preflight).

### Screen 4 — Payment Detail, terminal state
- **Visible state:** `RECOVERY_SUCCEEDED` ("Attempt succeeded — ₹8,420.00 recovered"), mission
  badge `RECOVERED`.
- **Backend event:** a real, unforced outcome from `SimulatorAdapter`'s dice roll against this
  payment's seeded latents — very likely SUCCESS, but genuinely possible to be a second FAILED
  (see §4), in which case the timeline shows one more replan cycle before this screen.
- **Narration:** *"And it recovered — ₹8,420 back."* (Avoid committing to "second strategy"
  specifically on camera; say "eventually" or "on this attempt" if a third round happened.)
- **Duration:** ~10s.

### Screen 5 — AI Recommendation → Fusion section (same page, scroll)
- **Visible state:** deterministic winner, AI's recommended action, confidence, fusion reason
  (e.g. "AI recommendation matches the deterministic winner" — the honest, accurate reason here,
  given the cost skew described in §4).
- **Technical significance:** the exact provenance a skeptical judge would ask for — this is not
  a black box.
- **Narration:** *"Every fusion decision is recorded — what the AI recommended, what the
  deterministic engine decided, and why."*
- **Duration:** ~15s.

### Screen 6 — Payment Detail, `safety_escalation` mission (second, pre-triggered payment)
- **Visible state:** `AI_RECOMMENDATION` ("Recommends ESCALATE"), immediately followed by
  `POLICY_ESCALATED`. No execution events at all.
- **Backend event:** real `AIRiskSignalEscalationRule` firing on a real closed-set risk flag.
- **Technical significance:** proves the authority boundary — a risk signal halts execution,
  unconditionally, before the EVI floor is even checked.
- **Business significance:** "the system says no to itself when something looks wrong — nobody
  has to catch that manually."
- **Narration:** *"AI can flag risk. It can't authorize money movement — a deterministic rule
  does that, and here it says stop."*
- **Duration:** ~20s.

### Screen 7 — Experiments (`/experiments`)
- **Visible state:** the 5-seed table, the headline incremental-recovery figure.
- **Narration:** *"Across five independent 10,000-payment simulations, compared against a
  baseline that obeys the same compliance rules RecoveryOS does, this pattern produced
  +₹73,181.78 mean incremental recovery — positive in all five seeds."*
- **Duration:** ~25s.
- **Do not say:** that the AI caused this number. It's the whole system's — see §6.

### Screen 8 — Audit / architecture beat
- **Visible state:** the Audit Explorer chain for the hero payment, OR a brief cut to the
  architecture diagram from the README if that reads better on camera.
- **Narration:** *"Underneath this: idempotent execution, persisted mission state, a scheduler
  that survives a crash without duplicating work, and an AI layer that's bounded by a closed
  schema it can never smuggle an execution parameter through."*
- **Duration:** ~20s.

### Screen 9 — Close (Control Tower)
- **Visible state:** the hero mission now showing `RECOVERED`.
- **Narration:** *"A failed payment isn't the end of the transaction. It's the beginning of a
  recovery mission."*
- **Duration:** ~5s, hold.

---

## 6. AI demonstration strategy

Four distinct claims, never blurred:

| Claim | Evidence type | What proves it |
|---|---|---|
| The architecture exists and is bounded | Architecture proof | `RecoveryRecommendation` schema, `_apply_ai_fusion`, the authority hierarchy — README §6-§9 |
| The mechanism works exactly as designed | Controlled-test proof | `test_tie_break_applies_for_near_tied_policy_allowed_recommendation`, `test_risk_flag_escalates_regardless_of_strongly_positive_evi` |
| The real Gemini path works end to end | Real-Gemini proof | `recover_via_replan`'s live investigation (shown on camera); separately, `tests/evaluation/artifacts/ai_ablation_results.json` (4 payments, 2 real recommendations, 0 unsafe deltas) |
| The whole system recovers more revenue | Benchmark proof | The 5-seed compliance-aware campaign — never attributed to AI specifically |

**On camera, say exactly this and nothing stronger:** *"AI proposes recovery intelligence. It does
not authorize money movement — a deterministic engine does."* Do not say "Gemini decided this
payment should retry" — say "RecoveryOS investigated this payment" (true regardless of whether
that specific round used a live Gemini call or the deterministic fallback, which is itself an
honest and important property to be able to say).

**Use a real, live Gemini call for the actual recording** — `recover_via_replan`, run for real,
during the final take(s). Do **not** re-run it many times during technical/timing rehearsal;
rehearse the navigation and pacing using `safety_escalation` (zero Gemini cost, fully
deterministic, instant) and budget your real Gemini quota (20 requests/day/model on the free
tier) for a small number of full dress rehearsals plus the actual recording. Each
`recover_via_replan` run costs roughly 2–6 real calls (two investigation rounds, up to 3 calls
each). That gives you room for a handful of real runs in one day, not dozens.

---

## 7. Razorpay strategy

**Recommendation: simulator-backed, not live Razorpay, for the recording.**

The real integration exists and was independently verified for order creation
(`integrations/razorpay/adapter.py`'s own docstring: a genuine order was created against a real
Razorpay test-mode key). But webhook-driven resolution back to SUCCESS/FAILED has never been
proven the same way — it needs a public URL (ngrok or a real deployment) registered in the
Razorpay dashboard, which this repo has never set up or tested. Introducing that live, untested
dependency into a one-shot recording is exactly the kind of risk §9 exists to avoid.

`world_changed` drives its outcome through the *identical* `reconcile_pending_recovery` function a
real webhook would call — so demonstrating it exercises the real reconciliation code path, just
with a deterministic trigger instead of a live external service. `recover_via_replan` does NOT use
this path at all (see §0/§4) — its replan/recovery happens entirely through the real Phase 13
closed loop (`execution_worker` + `retry_scheduler`), no webhook-shaped code involved. Keep these
claims separate on camera: for `world_changed`, it's fair to say *"the outcome resolution here runs
through the same code a real Razorpay webhook uses"*; for `recover_via_replan`, say instead
*"this replan happens through the real production scheduler, the same one that would fire for a
genuine failed payment — nothing here is simulated timing."*

If a live Razorpay webhook demonstration is wanted for a future recording, that's real,
independent follow-up work (ngrok tunnel + dashboard webhook registration + a fresh end-to-end
verification) — not something to attempt for the first time during final recording prep.

---

## 8. Demo reset / rehearsal procedure

Using `demo.sh` (already exists, wraps the commands below) plus the AI-fusion override:

```bash
# 1. Full clean slate — destroys the demo Postgres/Redis volumes for THIS
#    compose project only (docker compose derives the project name from
#    the current directory; run from the repo root, nothing outside this
#    project's own named volumes is touched).
docker compose down -v

# 2. Bring the stack up WITH the AI-fusion override (required for both
#    demo scenarios) and a real Gemini key already in .env.
AI_RECOMMENDATION_FUSION_ENABLED=true docker compose \
  -f docker-compose.yml -f docker-compose.override.ai_fusion.yml up -d --build

# 3. Wait for health, seed a merchant/key (demo.sh automates this).
./demo.sh

# 4. Trigger the hero scenario (use the API key demo.sh printed).
curl -X POST http://localhost:8000/v1/simulate/scenario \
  -H "X-API-Key: <key>" -H "Content-Type: application/json" \
  -d '{"scenario": "recover_via_replan"}'

# 5. (Separately, for the safety beat) trigger this ahead of time so it's
#    already resolved when you cut to it on camera:
curl -X POST http://localhost:8000/v1/simulate/scenario \
  -H "X-API-Key: <key>" -H "Content-Type: application/json" \
  -d '{"scenario": "safety_escalation"}'

# 6. Start the dashboard.
cd apps/dashboard && npm install && npm run dev
```

**This step down `docker compose down -v` is destructive to whatever's in the demo Postgres/Redis
— confirm nothing you care about is only there before running it** (the benchmark artifacts in
§0 live in `tests/evaluation/artifacts/` as files, not in this database, so they're unaffected
either way — but confirm this for yourself before the final pass, don't just trust this document).

**Rehearsal schedule** (matching the 3-run structure already proposed):
1. **Technical run** — run the full sequence above once, end to end, no camera, no script. Confirm
   the mission actually reaches `RECOVERED` and `ESCALATED` respectively.
2. **Timing run** — `docker compose down -v` + reset, run the actual narration script against the
   real timing. Note anywhere it runs long.
3. **Camera rehearsal** — one full dry run as if recording. No pauses, no improvisation. If
   anything takes longer than the storyboard's duration, fix the *demo* (pre-trigger earlier,
   adjust narration pacing) — don't plan to stare at a spinner during the real take.

---

## 9. Failure contingency plan

| If this happens | Do this |
|---|---|
| Gemini call times out / quota exhausted mid-recording | The system already falls back to a deterministic diagnosis automatically — the mission still proceeds. Narrate: *"and if the AI path is ever unavailable, the deterministic fallback keeps the mission moving — never a hang."* This is true and turns a failure into a feature. |
| `recover_via_replan` takes a 3rd round instead of 2 (attempt 2 also organically fails) | Real, expected, not a bug (§4) — just slower by ~10-15s and one more replan cycle on the timeline. If pre-triggering ahead of the camera (recommended, §9 timing row below), this is invisible either way. If triggering live, don't panic-narrate; the mission still resolves. |
| `retry_scheduler` container isn't running / hasn't picked up the reschedule | Check `docker compose logs retry_scheduler` and `docker compose ps` before recording — this container must be healthy for `recover_via_replan`'s replan to ever fire (see §14 preflight). If it happens live, cut to the pre-triggered `safety_escalation` mission instead and explain the replan beat using the already-rehearsed timing-run footage/description. |
| Worker delayed / mission stuck in an intermediate state | Wait one more 3s poll cycle before reacting on camera — the UI catches up automatically, no refresh needed. |
| UI looks stale | Confirm the browser tab is actually the dashboard's own polling page, not a cached screenshot from an earlier take — reload once, off camera, before starting. |
| Mission takes longer than storyboarded | Pre-trigger the hero scenario ~60-90 seconds before "Action" so the interesting middle section is already ready to show when the camera gets there — don't trigger it live on camera unless the timing run proved it's consistently fast enough. |
| Browser navigation goes wrong | Have every URL (`/`, `/payments/{id}`, `/experiments`, `/audit/{id}`) typed and ready in a second tab/notes file, not memorized live. |
| Real Razorpay/external service fails | N/A by design — see §7, nothing in the recording depends on a live external service. |

---

## 10. Presenter script

*(Word-for-word draft — adjust to your own voice, but keep the claims exactly as stated.)*

**[0:00]** "A failed payment isn't the end of the transaction. Most systems treat it that way —
retry a few times, then give up. RecoveryOS treats it as the beginning of a recovery mission."

**[0:30]** "This is the Control Tower. Every number here — revenue at risk, recovered, active
missions — is live from the database. RecoveryOS isn't watching payments, it's operating on them."

**[0:50]** "Watch what happens when a payment fails right now." *(trigger, or cut to
pre-triggered)*

**[1:15]** "The moment it failed, RecoveryOS opened a Recovery Mission and started investigating —
gathering real evidence about this customer and this bank, not just reading a failure code."

**[1:35]** "It produced a recommendation, a deterministic policy and expected-value engine
evaluated it, and authorized the first recovery attempt."

**[1:55]** "That attempt failed. Watch what RecoveryOS does next — not retry the same thing, but
notice, gather fresh evidence, and replan."

**[2:20]** "And it recovered — ₹8,420 back." *(If the timeline shows a third round because attempt
2 also failed organically, that's fine — say "it kept trying, and it recovered" instead. Don't
script a fixed attempt count; narrate what's actually on screen — see §4.)*

**[2:30]** "Every one of those decisions is fully auditable — here's exactly what the AI
recommended, what the deterministic engine decided, and why."

**[2:50]** "And here's the boundary that makes this safe. AI proposes recovery intelligence. It
does not authorize money movement. When the AI flags real risk — like this payment, where the
evidence pointed to a fraud-probing pattern — a deterministic rule halts everything before a
single rupee moves. No execution. No exception."

**[3:20]** "This is the difference between a retry system and a recovery system. A retry system
asks: should I try again? RecoveryOS asks: given what just happened, what should I do now? That's
what you just watched — fail, observe, replan, recover."

**[4:05]** "So does this actually create value? We tested it across five independent 10,000-
payment simulations, against a baseline that obeys the exact same compliance rules RecoveryOS
does — not a weaker strawman. Mean incremental recovery: +₹73,181.78. Positive in all five seeds."

**[4:35]** "Underneath all of this: idempotent execution, so nothing double-charges. Persisted
mission state, so a crash never loses track of a payment mid-recovery. And an AI layer that's
structurally incapable of choosing an amount, an ID, or a provider parameter — those never leave
the deterministic engine."

**[4:55]** "A failed payment isn't the end of the transaction. It's the beginning of a recovery
mission. That's RecoveryOS."

---

## 11. Judge comprehension map

- **After 30 seconds:** RecoveryOS treats payment failure as the start of something, not the end.
- **After 2 minutes:** they've watched one real payment get investigated, authorized, fail, replan,
  and recover — not a static demo, an actual closed loop.
- **After 4 minutes:** they understand the AI/deterministic boundary precisely (AI recommends,
  never authorizes), and they've seen the safety case (risk flag halts execution).
- **After 5 minutes:** they have a specific, honestly-qualified revenue number, and know it's
  independently reproducible from the repository, not a claimed-but-unverifiable figure.

---

## 12. Technical credibility map

| Claim in the script | Backing |
|---|---|
| "investigating — gathering real evidence" | `services/diagnosis_engine/investigator.py`, `TOOL_REGISTRY` (6 real tools) |
| "deterministic policy and expected-value engine evaluated it" | `services/policy_engine/rules.py` (11 rules), `services/recovery_engine/evi.py` |
| "notice, gather fresh evidence, and replan" | `test_replan_produces_different_recommendation.py`; live via `workers/retry_scheduler.py::run_once` |
| "fully auditable" | `decision_fusion_trace` table, `GET /v1/audit/{payment_id}` |
| "AI does not authorize money movement" | `RecoveryRecommendation` schema (no execution fields), `test_execution_boundary_never_references_recommendation_fields` |
| "a deterministic rule halts everything" | `AIRiskSignalEscalationRule`, `services/policy_engine/rules.py` |
| "idempotent execution" | `idempotency_key` + advisory lock, `test_provider_duplicate_response.py` |
| "persisted mission state... survives a crash" | `services/recovery_engine/mission.py`, scheduler lease/reclaim (`test_retry_scheduler.py`) |
| "+₹73,181.78... positive in all five seeds" | `tests/evaluation/artifacts/multi_seed_compliance_aware_aggregate.json` |

---

## 13. Demo risk ranking

| Risk | Severity | Mitigation |
|---|---|---|
| Gemini free-tier quota exhausted before the real take | **P0** | Rehearse navigation/timing with `safety_escalation` (zero quota cost); budget real `recover_via_replan` runs (2-6 calls each) against the 20/day limit; do the real take early in the day, not after burning quota on rehearsals |
| Stale/messy demo DB visible on camera | P0 | `docker compose down -v` + fresh seed before the final recording session — see §8 |
| `execution_worker` not running / job never processed | P0 | Confirm `docker compose ps` shows all workers healthy before every rehearsal and before recording |
| Mission timing runs longer than storyboarded | P1 | Pre-trigger 60-90s before the camera needs it, per §9 |
| `recover_via_replan` takes a real 3rd round (attempt 2 also fails) instead of the common 2-round case | P2 | Real and understood, not a bug (§4) — adds ~10-15s. Pre-triggering (above) absorbs this either way; only a risk if triggering live on a tight cue |
| Browser shows dev artifacts (console errors, raw IDs, localhost debug params) | P1 | Clean browser profile, no devtools open, check §14 checklist |
| `AI_RECOMMENDATION_FUSION_ENABLED` accidentally false on recording day | P0 | Explicit preflight check — see §14 |
| Live Razorpay dependency introduced late and fails | P0 (if attempted) | Don't attempt it — §7 |
| Confusing the compliance-blind vs compliance-aware baseline on camera | P1 | Script only references the compliance-aware number; don't ad-lib the other one |
| Presenter overclaims AI causality live | P0 | Use the script's exact phrasing in §10, rehearsed |

---

## 14. Final recording checklist (preflight)

```text
DEMO PREFLIGHT

Environment
[ ] docker compose down -v run, fresh volumes confirmed
[ ] Stack brought up WITH docker-compose.override.ai_fusion.yml
[ ] AI_RECOMMENDATION_FUSION_ENABLED=true confirmed (curl a scenario, expect no 409)
[ ] docker compose ps — postgres, redis, api, event_processor, pipeline_orchestrator,
    execution_worker, retry_scheduler all healthy/running
[ ] GEMINI_API_KEY present and not exhausted (check quota before the session)
[ ] Merchant + API key freshly seeded (demo.sh)

Data
[ ] Control Tower shows 0 stale historical missions/payments before triggering the hero scenario
[ ] Hero scenario (recover_via_replan) triggered and reaches RECOVERED in a rehearsal run today
[ ] Safety scenario (safety_escalation) pre-triggered, resolved to ESCALATED, payment_id noted

Frontend
[ ] Fresh browser profile/window, no devtools, no unrelated tabs
[ ] No console errors on Control Tower / Payment Detail / Experiments / Audit pages
[ ] All 4 navigation URLs typed and ready (not memorized live)
[ ] Zoom/window size checked — no unnecessary scrolling for key cards

Content
[ ] Experiments page numbers match tests/evaluation/artifacts/multi_seed_compliance_aware_aggregate.json exactly
[ ] Narration script rehearsed at least once at real timing (camera rehearsal, §8)
[ ] No API keys, secrets, or .env content visible on any screen to be recorded

Benchmark integrity
[ ] tests/evaluation/artifacts/ untouched by any of the above
[ ] git status clean except intended demo-only changes (none expected — this is a docker/DB
    operation, not a code change)
```

---

## 15. Final verdict

**B — READY AFTER SMALL DEMO PREPARATION.**

The demo *mechanism* is not the gap — `simulate.py`'s scenario engine is already real, already
wired to the actual production code paths, and already more honest than a typical scripted demo
would be. Live rehearsal already found and fixed the two real bugs that existed in that engine
(§0/§4) — this is no longer an unverified plan, both hero scenarios have been confirmed to resolve
correctly against the actual running stack. What's left before recording is purely *preparation*,
not engineering:

1. A fresh demo DB reset (§8) — not yet done as of this revision.
2. One full rehearsal at real timing, to confirm the durations in §2/§5 hold up in practice under
   the corrected mechanism (some are still estimates, now from a code path that's been verified
   live but not stopwatch-timed end to end).
3. Confirming Gemini quota is available on recording day (§13's P0 risk).
4. ~~Actually capturing the screenshots the README already flags as missing~~ — **done**: real,
   live-captured screenshots are in `docs/images/` and embedded in the README.

Nothing in this runbook requires new engineering. The recommended next step is exactly what was
proposed before: run the clean-room reset, do the technical rehearsal, and confirm the corrected
timings above hold up end to end before the final recording.
