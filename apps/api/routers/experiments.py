"""
Experiments router — GET /v1/experiments/{run_id} (PRD §47).

Two real, non-fabricated data sources, selected by `run_id`:

  - run_id == "live": aggregates THIS merchant's own recovery_ledger +
    baseline_runs rows (populated by services/pipeline/ledger.py and
    services/pipeline/baseline.py since Phase 7) for every synthetic
    payment this merchant has actually processed through the live
    pipeline. Empty/zeroed if the merchant hasn't run any synthetic
    traffic yet -- never a fabricated placeholder.
  - run_id == "phase8-baseline": serves the real Phase 8 multi-seed
    replication study (tests/evaluation/artifacts/multi_seed_results.json,
    docs/phase8_priority0_multi_seed_baseline.md) -- 5 independent
    10,000-payment runs, read verbatim off disk, never regenerated or
    guessed at request time.

Any other run_id is a real 404, not a fabricated empty result.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies.auth import verify_api_key
from recoveryos.database import get_app_session
from recoveryos.models import Merchant
from services.pipeline.baseline import (
    PIPELINE_BASELINE_EXPERIMENT_ID,
    PIPELINE_BASELINE_FAIR_EXPERIMENT_ID,
)

router = APIRouter()

MULTI_SEED_ARTIFACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "evaluation"
    / "artifacts"
    / "multi_seed_results.json"
)


async def _live_experiment(merchant: Merchant, session: AsyncSession) -> dict:
    row = (
        (
            await session.execute(
                text(
                    """
                SELECT
                    COALESCE(SUM(br.recovered_amount_paise), 0) AS baseline_recovered_paise,
                    COALESCE(SUM(rl.actual_recovery_paise), 0) AS recoveryos_recovered_paise,
                    COALESCE(SUM(rl.incremental_recovery_paise), 0) AS incremental_recovery_paise,
                    COUNT(DISTINCT rl.payment_id) AS dataset_size,
                    COUNT(DISTINCT rl.payment_id) FILTER (WHERE rl.actual_recovery_paise > 0) AS recovered_count,
                    COUNT(DISTINCT rl.payment_id) FILTER (WHERE rl.expected_recovery_paise > 0) AS interventions,
                    COUNT(DISTINCT rl.payment_id) FILTER (
                        WHERE rl.expected_recovery_paise > 0 AND rl.actual_recovery_paise = 0
                    ) AS unnecessary_interventions
                FROM recovery_ledger rl
                JOIN payments p ON p.payment_id = rl.payment_id
                LEFT JOIN baseline_runs br
                    ON br.payment_id = rl.payment_id AND br.experiment_id = :exp_id
                WHERE p.merchant_id = :merchant_id AND rl.baseline_outcome IS NOT NULL
                """
                ),
                {"exp_id": PIPELINE_BASELINE_EXPERIMENT_ID, "merchant_id": merchant.merchant_id},
            )
        )
        .mappings()
        .one()
    )

    # Domain Audit finding #6's decomposition -- a SEPARATE, scoped query,
    # not reused from the one above: the three quantities being subtracted
    # (baseline, RecoveryOS, fair baseline) must all be summed over the
    # EXACT SAME payment set (an INNER join against BOTH baseline
    # experiment ids), or the arithmetic silently compares different
    # populations -- caught live-testing this exact endpoint: an earlier
    # version LEFT-joined the fair baseline separately, producing a wildly
    # wrong negative "attributable_to_more_attempts" once fair-baseline
    # coverage was partial (computed for only 1 of 341 payments) while the
    # old baseline sum still covered the full 341.
    fair_row = (
        (
            await session.execute(
                text(
                    """
                SELECT
                    COALESCE(SUM(br.recovered_amount_paise), 0) AS baseline_recovered_paise,
                    COALESCE(SUM(rl.actual_recovery_paise), 0) AS recoveryos_recovered_paise,
                    COALESCE(SUM(fbr.recovered_amount_paise), 0) AS fair_baseline_recovered_paise,
                    COUNT(DISTINCT fbr.payment_id) AS fair_baseline_dataset_size
                FROM recovery_ledger rl
                JOIN payments p ON p.payment_id = rl.payment_id
                JOIN baseline_runs br
                    ON br.payment_id = rl.payment_id AND br.experiment_id = :exp_id
                JOIN baseline_runs fbr
                    ON fbr.payment_id = rl.payment_id AND fbr.experiment_id = :fair_exp_id
                WHERE p.merchant_id = :merchant_id AND rl.baseline_outcome IS NOT NULL
                """
                ),
                {
                    "exp_id": PIPELINE_BASELINE_EXPERIMENT_ID,
                    "fair_exp_id": PIPELINE_BASELINE_FAIR_EXPERIMENT_ID,
                    "merchant_id": merchant.merchant_id,
                },
            )
        )
        .mappings()
        .one()
    )

    dataset_size = int(row["dataset_size"])
    baseline_recovered = int(row["baseline_recovered_paise"])
    recoveryos_recovered = int(row["recoveryos_recovered_paise"])
    recovery_rate_bps = (
        (int(row["recovered_count"]) * 10_000) // dataset_size if dataset_size > 0 else 0
    )

    result = {
        "run_id": "live",
        "source": "recovery_ledger + baseline_runs for this merchant's own synthetic traffic",
        "dataset_size": dataset_size,
        "baseline": {
            "recovered_paise": baseline_recovered,
            "interventions": dataset_size,  # baseline heuristic evaluates every payment
        },
        "recoveryos": {
            "recovered_paise": recoveryos_recovered,
            "interventions": int(row["interventions"]),
            "unnecessary_interventions": int(row["unnecessary_interventions"]),
        },
        "incremental_recovery_paise": int(row["incremental_recovery_paise"]),
        "recovery_rate_bps": recovery_rate_bps,
    }

    # Domain Audit finding #6: only present when the fair (same-attempt-
    # budget) baseline has actually been computed for at least one payment
    # in THIS merchant's dataset -- never a fabricated decomposition.
    #
    # IMPORTANT: every number inside `fair_comparison` (including its own
    # `scoped_incremental_recovery_paise`) is computed from `fair_row`
    # ONLY -- i.e. summed over exactly the payments that have recovery_ledger
    # + BOTH baseline experiment rows. It is NOT the same population as the
    # top-level `incremental_recovery_paise` above whenever fair-baseline
    # coverage is partial (e.g. the fair baseline has only been computed
    # for 1 of a merchant's 341 payments so far) -- comparing across the
    # two different-sized populations would silently produce nonsense
    # (caught live-testing this exact endpoint). The identity that DOES
    # hold, always, by construction:
    #   scoped_incremental_recovery_paise == attributable_to_more_attempts_paise
    #                                       + attributable_to_better_decisions_paise
    fair_baseline_dataset_size = int(fair_row["fair_baseline_dataset_size"])
    if fair_baseline_dataset_size > 0:
        scoped_baseline_recovered = int(fair_row["baseline_recovered_paise"])
        scoped_recoveryos_recovered = int(fair_row["recoveryos_recovered_paise"])
        fair_baseline_recovered = int(fair_row["fair_baseline_recovered_paise"])
        attributable_to_more_attempts = fair_baseline_recovered - scoped_baseline_recovered
        attributable_to_better_decisions = scoped_recoveryos_recovered - fair_baseline_recovered
        result["fair_comparison"] = {
            "description": (
                "Decomposes incremental recovery into 'we tried more times' vs "
                "'we chose better actions', by giving the naive baseline the SAME attempt "
                "budget (policy_configs.max_retries) RecoveryOS itself is allowed -- see "
                "services/pipeline/baseline.py:compute_and_persist_fair_baseline_run. All "
                "figures in this block are scoped to only the payments with a fair-baseline "
                "row computed -- may be a SUBSET of dataset_size above if fair-baseline "
                "coverage isn't complete yet."
            ),
            "dataset_size": fair_baseline_dataset_size,
            "scoped_baseline_recovered_paise": scoped_baseline_recovered,
            "scoped_recoveryos_recovered_paise": scoped_recoveryos_recovered,
            "scoped_incremental_recovery_paise": scoped_recoveryos_recovered
            - scoped_baseline_recovered,
            "fair_baseline_recovered_paise": fair_baseline_recovered,
            "attributable_to_more_attempts_paise": attributable_to_more_attempts,
            "attributable_to_better_decisions_paise": attributable_to_better_decisions,
        }

    return result


def _phase8_baseline_experiment() -> dict:
    if not MULTI_SEED_ARTIFACT_PATH.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Phase 8 multi-seed artifact not found at {MULTI_SEED_ARTIFACT_PATH}",
        )
    with MULTI_SEED_ARTIFACT_PATH.open() as f:
        seeds: list[dict] = json.load(f)

    incrementals = [s["incremental_recovery_paise"] for s in seeds]
    mean_incremental = statistics.mean(incrementals)
    stdev_incremental = statistics.stdev(incrementals) if len(incrementals) > 1 else 0.0
    # 95% CI via t-distribution critical value for df=len-1 (hardcoded
    # 2.776 for df=4, matching docs/phase8_priority0_multi_seed_baseline.md's
    # own reported CI exactly -- not recomputed with a different method
    # that could silently disagree with the audited doc).
    t_critical_df4 = 2.776
    margin = (
        t_critical_df4 * (stdev_incremental / (len(incrementals) ** 0.5)) if len(seeds) > 1 else 0.0
    )

    return {
        "run_id": "phase8-baseline",
        "source": str(MULTI_SEED_ARTIFACT_PATH),
        "dataset_size": seeds[0]["failed_payments"] if seeds else 0,
        "seeds": seeds,
        "baseline": {
            "recovered_paise": round(statistics.mean([s["baseline_total_paise"] for s in seeds])),
        },
        "recoveryos": {
            "recovered_paise": round(statistics.mean([s["recoveryos_total_paise"] for s in seeds])),
            "recovery_rate_bps": round(
                statistics.mean([s["recovery_rate"] for s in seeds]) * 10_000
            ),
            "intervention_rate_bps": round(
                statistics.mean([s["intervention_rate"] for s in seeds]) * 10_000
            ),
            "unnecessary_intervention_rate_bps": round(
                statistics.mean([s["unnecessary_intervention_rate"] for s in seeds]) * 10_000
            ),
        },
        "incremental_recovery_paise_mean": round(mean_incremental),
        "incremental_recovery_paise_stdev": round(stdev_incremental),
        "incremental_recovery_95ci_paise": [
            round(mean_incremental - margin),
            round(mean_incremental + margin),
        ],
    }


@router.get("/{run_id}", summary="Evaluation: RecoveryOS vs baseline comparison")
async def experiment_results(
    run_id: str,
    merchant: Merchant = Depends(verify_api_key),
    session: AsyncSession = Depends(get_app_session),
):
    if run_id == "live":
        return await _live_experiment(merchant, session)
    if run_id == "phase8-baseline":
        return _phase8_baseline_experiment()

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=(
            f"Unknown run_id={run_id!r}. Valid values: 'live' (this merchant's own "
            "recovery_ledger/baseline_runs data) or 'phase8-baseline' (the real Phase 8 "
            "multi-seed replication study)."
        ),
    )
