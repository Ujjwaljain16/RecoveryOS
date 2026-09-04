# Priority 0 — Multi-Seed Baseline v1

> **Superseded by the compliance-aware study** — see README §9/§10 and
> [`tests/evaluation/artifacts/multi_seed_compliance_aware_aggregate.json`](../tests/evaluation/artifacts/multi_seed_compliance_aware_aggregate.json)
> for the current headline number. Kept for the fairness-methodology narrative (README §10), not as
> the current result.

TRD §7's headline number (Phase 8, `tests/evaluation/report.md`) was computed from a single
seed (42): **+₹42,491.88 incremental recovery**. Before touching the engine further, this
records whether that number is representative or a lucky/unlucky draw — run across 5
independent seeds, each a full, fresh 10,000-payment canonical dataset processed through the
complete live pipeline (identical code, identical config, only the seed differs).

Raw per-seed results: [`tests/evaluation/artifacts/multi_seed_results.json`](../tests/evaluation/artifacts/multi_seed_results.json).
Generation script: [`tests/evaluation/multi_seed_runner.py`](../tests/evaluation/multi_seed_runner.py).

**Methodology note:** each seed's run pins the diagnoser to a guaranteed-invalid key
(`docker-compose.override.baseline.yml`), matching the original Phase 8 methodology exactly —
LLM availability must not be a confound in a study whose whole point is measuring seed-to-seed
variance in the same configuration already reported against.

## The headline number, across 5 seeds

| Seed | Recovery rate | Revenue recovered | Incremental recovery | Intervention rate | Unnecessary intervention rate | Policy blocks | Root-cause accuracy (committed) | Abstention rate | Wall-clock latency/payment |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 43.55% | ₹10,71,198.26 | +₹73,408.23 | 94.72% | 82.85% | 52 | 87.74% | 73.50% | 723ms |
| 2 | 42.15% | ₹10,68,670.08 | −₹11,054.40 | 96.15% | 86.76% | 39 | 89.59% | 73.45% | 643ms |
| 3 | 42.46% | ₹9,79,491.20 | +₹1,04,413.27 | 95.84% | 86.75% | 40 | 88.21% | 76.17% | 654ms |
| 4 | 43.24% | ₹9,90,428.06 | +₹1,32,339.00 | 96.80% | 84.90% | 32 | 90.76% | 75.08% | 685ms |
| 5 | 42.48% | ₹9,97,180.46 | +₹52,181.97 | 96.72% | 84.44% | 32 | 89.00% | 68.37% | 745ms |

| Statistic | Incremental recovery |
|---|---:|
| Mean | **+₹70,257.61** |
| Median | +₹73,408.23 |
| Std. dev. | ₹54,701.48 |
| **95% CI** (t-distribution, df=4) | **[+₹2,347.65, +₹1,38,167.58]** |
| Worst seed | seed 2, **−₹11,054.40** |
| Best seed | seed 4, +₹1,32,339.00 |

## Verdict: real, but noisy — not a fabricated or lucky number

**The 95% CI does not cross zero.** One of five seeds (seed 2) landed negative, and a reader
handed only that seed would have concluded RecoveryOS makes things worse. But across 5
independent draws the interval is entirely positive — the incremental-recovery effect is
genuinely there, not an artifact of seed 42's particular randomness. It is, however, a **noisy**
effect: coefficient of variation ≈ 78%, and the original Phase 8 headline (+₹42,491.88) sits
comfortably inside this range rather than being either best-case or worst-case cherry-picking.

**Every other metric replicated far more tightly than the headline number did** — these are
stable properties of the system, not seed-42 quirks:

- Recovery rate: 42.15%–43.55% (tight, ~1.4pp spread)
- Intervention rate: 94.72%–96.80%
- Unnecessary intervention rate: 82.85%–86.76% (confirms the original 87.74% finding wasn't an
  outlier)
- Root-cause accuracy (committed diagnoses only): 87.74%–90.76% (confirms the original 91.03%)
- Abstention rate: 68.37%–76.17% (confirms the original ~75.4%)

The one number that genuinely swings — including sign — is the incremental-recovery headline
itself, because it's a difference of two stochastic outcome draws (RecoveryOS's actual outcome
vs. baseline's independent counterfactual draw) at the individual-payment level, aggregated over
only 950 failures per seed. That's a real, disclosed limitation of measuring this quantity at
this sample size, not a bug.

## What this means for anything built on top of the headline number

Any claim of the form "RecoveryOS recovers +₹X" should be stated as **"+₹70,258 on average,
95% CI [₹2,348, ₹1,38,168], across 5 independent 10k-payment runs"** — not a single point
estimate from one seed. This is the number to defend if asked "is that real or did you get
lucky."

## Addendum — the current campaign's own retained diagnostic comparator

The above is the older single-seed-vs-multi-seed lineage. The CURRENT headline campaign
(`multi_seed_compliance_aware_aggregate.json`, README §9) retains its own weaker diagnostic
comparator (`compliance_blind_fair_baseline_DIAGNOSTIC_ONLY`, same rationale as this whole
document: measure a real gap honestly rather than assume). Stated in full here so it exists in
prose somewhere, not just derivable from the raw JSON:

RecoveryOS loses to that comparator in all 5 seeds — mean **−₹1,42,189**, ranging −₹1,12,254 to
−₹2,05,358 per 10,000-payment run (`incremental_recoveryos_vs_compliance_blind_fair_paise_DIAGNOSTIC_ONLY`
per seed in the same artifact). Expected, not a red flag: this comparator is allowed to fire
`RETRY_NOW` during NPCI peak windows, past `max_retries`, above the RBI AFA threshold — real
regulatory ceilings RecoveryOS's actual policy chain (`services/policy_engine/rules.py`, 12 rules)
obeys and this diagnostic doesn't. A comparator allowed to break rules a real deployment would be
fined for isn't a fair yardstick, which is exactly why the compliance-aware baseline, not this
one, is the real headline comparison.
