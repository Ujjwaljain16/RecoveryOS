# Phase 11 AI Ablation — What Was Measured, and What Wasn't

This doc is referenced from `recoveryos/config.py`'s `ai_tie_break_tolerance_bps`
and `ai_tie_break_min_confidence` docstrings and didn't exist yet — written as
part of closing the AI Architecture Gap Audit's findings. It states plainly
what real-model evidence exists for the Phase 11 bounded AI-recommendation
fusion (`services/recovery_engine/orchestrator.py`'s `_apply_ai_fusion`, see
`docs/TRD.md` §3.5), and — just as importantly — what does not, so nothing
here gets overclaimed in front of a skeptical evaluator.

## The two constants this ablation is about

Both gates on the AI tie-break path (`recoveryos/config.py`) were fixed
**before** any measurement, deliberately, so neither could be tuned post-hoc
to flatter a result:

- `ai_tie_break_tolerance_bps` (default 100 = 1%): how close an AI-recommended
  candidate's EVI must be to the deterministic winner's to be eligible for a
  tie-break. Swept at 0 / 100 / 500 bps as three separate arms below.
- `ai_tie_break_min_confidence` (default 0.5): the AI Architecture Gap Audit's
  P1 finding, closed. `RecoveryRecommendation.confidence` used to be
  persisted and displayed but never actually gated the tie-break decision —
  a 0.05-confidence recommendation and a 0.95-confidence one were equally
  eligible. **0.5 is a pre-committed engineering safety/quality floor chosen
  before any measurement — not a learned, benchmarked, or statistically
  optimized threshold.** The honest answer to "why 0.5?" is that it was fixed
  in advance specifically so this safety boundary is never tuned against the
  benchmark's own outcomes, not derived from one.

Neither constant has ever been adjusted after seeing a result.

## What actually ran

`tests/evaluation/ai_ablation_runner.py`, against a real Gemini API key
(`gemini-2.5-flash-lite`), same seed (`42`) and payment population held
constant across arms:

| Arm | `fusion_enabled` | `tolerance_bps` | Failed payments | AI recs obtained | Tie-break applied | Risk escalations | Unsafe deltas |
|---|---|---|---|---|---|---|---|
| `AI_OFF` | false | — | 0 | 0 | 0 | 0 | 0 |
| `AI_ON_tol_0` | true | 0 | 0 | 0 | 0 | 0 | 0 |
| `AI_ON_tol_100` | true | 100 | 4 | 2 | 0 | 0 | 0 |
| `AI_ON_tol_500` | true | 500 | — | — | — | — | — |

(Figures for the first three arms are the real values in
`tests/evaluation/artifacts/ai_ablation_results.json`.)

The `AI_ON_tol_500` arm never completed — the run stopped after
`AI_ON_tol_100` because the Gemini free-tier key's daily quota
(`GenerateRequestsPerDayPerProjectPerModel-FreeTier`, 20 requests/day/model)
was exhausted. Each real diagnosis costs 2–3 Gemini calls (1–2 investigation
rounds, `MAX_INVESTIGATION_ROUNDS=2`, plus one always-required finalize
call), so one key supports roughly **8 real diagnoses per day** — nowhere
near enough for four multi-payment arms in one sitting, and nowhere near
enough to reach statistical significance on the tie-break acceptance rate
(§ below) without either a paid tier or running across many days.

## What this evidence does prove

- **The real Gemini path works end-to-end.** 2 genuine, schema-validated
  `RecoveryRecommendation`s were obtained from live model calls and correctly
  persisted through to `recovery_recommendations` / `decision_fusion_trace`.
- **The safety invariant holds under real model output.** `ai_unsafe_deltas
  == 0` in every arm that ran — no AI-driven decision the deterministic
  engine would not have separately allowed occurred, and the AI never
  authorized an out-of-policy or out-of-enum action.
- **This mirrors, with real model output, exactly what the extensive
  mocked/structural test suite already proves the mechanism does when it
  fires** — see `tests/integration/test_ai_recommendation_bounded_influence.py`,
  `tests/unit/test_ai_recommendation_adversarial.py`,
  `tests/unit/test_ai_fusion.py`, and
  `tests/integration/test_replan_produces_different_recommendation.py`
  (the last one proves, with a mocked-but-differing LLM response, that a
  second investigation round genuinely changes the final decision — the
  causal property this real run's tiny sample is too small to demonstrate
  on its own).

## What this evidence does NOT prove

**In this run, `ai_recommendation_acceptance_rate` was 0.0% — the AI's
recommendation never actually changed the final decision.** With only 2 real
recommendations obtained, this is not evidence that the mechanism doesn't
work in practice (the mechanism-correctness claim is already established by
the structural tests above) — it is simply too small a sample to say
anything about how *often* a real near-tie or a real risk signal occurs in
production-shaped traffic. Do not read "0% acceptance" as "AI doesn't
matter" or as "AI works and just happened not to fire" — neither claim is
supported by N=2. The honest statement is: **mechanism proven, safety
proven, real-model behavioral frequency not yet measured at any meaningful
scale.**

Getting a statistically meaningful answer would require either a paid
Gemini tier or patience across many free-tier days — this was evaluated and
explicitly deferred (not pursued further in this pass) rather than
attempted with a workaround that would risk manufacturing a number.
