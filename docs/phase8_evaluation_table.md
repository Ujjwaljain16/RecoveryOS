# Phase 8 — PRD §35 Evaluation Table, Real Numbers

Replaces PRD §35's illustrative table with actual measured results from the canonical run
(see [phase8_canonical_run.md](phase8_canonical_run.md), [phase8_headline_number.md](phase8_headline_number.md)).
Every cell below is computed by SQL against `recovery_ledger`, `baseline_runs`, `policy_decisions`,
and `recoveries` — none are illustrative or adjusted.

## Definitions used (stated explicitly — several of PRD §35's terms have no single
existing code-level definition, so the operational definition used for each is recorded here)

- **Eligible failed payments** = all `payments` with `status='failed'` = **950**. Nothing is
  filtered out of the funnel before reaching a decision — `EligibilityRule`/`OptOutRule` produce
  `verdict='BLOCK'`, not an exclusion from the denominator (confirmed: every failed payment has
  exactly one `policy_decisions` row).
- **Intervention** = a `policy_decisions` row with `verdict='ALLOW'`. In this dataset every
  chosen action is `RETRY_NOW` (0 `ALT_ROUTE`/`REMINDER`/`ESCALATE`/`DO_NOTHING` rows), so ALLOW
  and "a real recovery attempt executed" coincide exactly (913 = 913, cross-checked against
  `recoveries` row count).
- **Unnecessary intervention** = an ALLOW intervention where `recovery_ledger.incremental_recovery_paise <= 0`
  — i.e. `actual_recovery_paise - baseline_recovered_amount_paise <= 0` for that payment, using
  the ledger's own pre-computed field (`services/pipeline/ledger.py:62`). No such metric was
  previously implemented anywhere in the codebase; this is the most direct reading of PRD's "no
  incremental benefit" using data the pipeline already produces.
- **Stopping rule compliance** — see the dedicated note below; reported as N/A with reasoning,
  not a fabricated percentage.

## The table

| Metric                      |            Baseline |           RecoveryOS |
| ---------------------------- | -------------------: | --------------------: |
| Payments evaluated (total dataset) |               10,000 |                10,000 |
| Eligible failed payments (metric denominator) |                   950 |                    950 |
| Revenue at risk               |         ₹23,59,678.31 |          ₹23,59,678.31 |
| Recovered revenue             |          ₹9,35,140.16 |           ₹9,77,632.04 |
| Recovery rate (recovered payments / eligible) |     379 / 950 = **39.89%** |     402 / 950 = **42.32%** |
| Revenue recovery rate (recovered revenue / revenue at risk) |    **39.63%** |    **41.43%** |
| Incremental recovery          |                     — |     **+₹42,491.88** (+4.5%) |
| Intervention rate (interventions / eligible) |   728 / 950 = **76.63%** |  913 / 950 = **96.11%** |
| Unnecessary intervention rate (no-benefit interventions / total interventions) |  N/A — see note below | 801 / 913 = **87.74%** |
| Average recovery value per intervention |     ₹1,284.53 |             ₹1,070.79 |
| Stopping rule compliance rate |         N/A — see note below | N/A — see note below |

## Honest notes on the two most surprising cells

**Intervention rate is *higher* for RecoveryOS (96.1%) than baseline (76.6%), not lower** —
the opposite direction from PRD's illustrative table (which showed 63% vs 100%). This is a real,
measured result, not adjusted to match the illustration: RecoveryOS's policy engine allows
retrying a broader set of failures than the baseline's blunt "skip PERMANENT / INSUFFICIENT_FUNDS
only" heuristic; only 37 payments (3.9%) get `BLOCK`ed by RecoveryOS's policy engine.

**Unnecessary intervention rate (87.7%) is much higher than PRD's illustrative 5%.** Breaking
down the 913 interventions by (RecoveryOS outcome × baseline counterfactual outcome):

| RecoveryOS | Baseline counterfactual | Count | Incremental Δ (paise) |
|---|---|---:|---:|
| Failed | NOT_ATTEMPTED | 190 | 0 |
| Failed | NOT_RECOVERED | 247 | 0 |
| Failed | RECOVERED | 74 | −23,996,846 |
| Succeeded | NOT_ATTEMPTED | 26 | +6,139,075 |
| Succeeded | NOT_RECOVERED | 86 | +24,988,137 |
| Succeeded | RECOVERED | 290 | 0 |

700 of 913 interventions (76.7%) land in a "both succeed" or "both fail" cell — mathematically
zero incremental benefit by definition, but this is **partly irreducible sampling noise**, not
evidence of policy waste: baseline and RecoveryOS resolve outcomes via independent stochastic
draws (`resolve_simulated_outcome`) even when acting on the same payment, so two independently-
resolved coin flips at similar probabilities will "agree" a large fraction of the time purely by
chance. The 74-payment "RecoveryOS failed, baseline would've succeeded" cell (−₹2.4L) is the
genuinely concerning one; the 112-payment "RecoveryOS succeeded, baseline wouldn't have" cells
(+₹3.1L combined) are the genuinely beneficial ones. Net effect across all 950 (interventions +
blocks): **+₹42,491.88**, i.e. the aggregate headline number remains the more meaningful signal
than this noisier per-payment "unnecessary" count. Reported as computed per the stated definition,
not softened.

**Baseline has no "unnecessary intervention rate" analog.** The baseline heuristic doesn't
target incremental benefit over anything — it *is* the naive comparator RecoveryOS is measured
against. There is no second, even-more-naive system to compare it to, so this cell is N/A rather
than a fabricated number.

**Stopping rule compliance rate is N/A for both columns in this run.** `recoveries.attempt_number`
is `1` for all 913 rows (100%), `stopping_rule_triggered` is `NULL` for all 913 rows, and every
chosen `action_type` in this dataset is `RETRY_NOW` — zero `RETRY_LATER` decisions were ever
made. This is architectural, not a bug: Phase 8's evaluation is a single-pass batch publish of
the entire canonical dataset's failed payments, executed within minutes — `RetryLimitRule`
(`services/policy_engine/rules.py:120-138`, `attempt_number <= max_retries` else `ESCALATE`)
only fires on a *subsequent* attempt of an already-attempted payment, and no payment in this run
ever reached a second attempt. **"Workflows requiring stopping" = 0** in this run, so the ratio
is undefined (0/0), not 100% or any other fabricated figure. Measuring this metric for real would
require either a multi-day live run (letting `RETRY_LATER`-scheduled follow-ups actually fire) or
a dedicated adversarial test that forces a payment past `max_retries` and checks the verdict —
neither has been done here.

## Provenance

Computed against the same canonical dataset and live-pipeline run as
[phase8_headline_number.md](phase8_headline_number.md) — no new run, no parameter changes.
