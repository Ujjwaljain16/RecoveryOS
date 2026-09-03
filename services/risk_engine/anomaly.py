"""
Rolling z-score anomaly detector — TRD §3.2, PRD §19.

    z = (observed_failure_rate - baseline_failure_rate) / baseline_std_dev

15-minute buckets. Baseline = mean/stdev of the SAME 15-minute-of-day bucket
on each of the trailing 7 days (a control-chart method, not an ML model —
deterministic, explainable in one sentence, TRD §3.2's own framing).

Severity:
    z > anomaly_z_score_high_threshold (3.0)   -> high    (cohort formed, suppress RETRY_NOW)
    z in (medium_threshold, high_threshold]     -> medium  (flagged, no suppression)
    otherwise                                   -> low     (not anomalous)
    current-bucket n < anomaly_min_sample_size  -> insufficient_data (skip z-score entirely)

Role boundary: this module runs entirely on `get_app_session()` (app_role).
It reads only bank/method/status/created_at from `payments` — none of that
is the ground-truth-restricted column set, so there's no reason to route
through the diagnoser's read-only connection here. The diagnoser_role
restriction (services/diagnosis_engine/) exists to keep `ground_truth_recoverable`
away from anything that feeds the LLM prompt, not to restrict this detector.

Schema note: `anomaly_windows` has no `cohort_id` column (TRD §2 doesn't
define one, and there's no separate `cohorts` table) — only `diagnoses.cohort_id`
does. Cohort ids are derived deterministically (uuid5) from
(scope_type, scope_entity, time_bucket) wherever needed, so any process
computing a cohort id for the same window agrees, without shared mutable
state. See `derive_cohort_id`.

Schema note 2: TRD §3.2's prose says "per (bank, method)" as if it were one
compound scope, but `anomaly_windows.scope_type` is a single dimension
(bank|method|merchant) per its own DDL and the UNIQUE index on
(scope_type, scope_entity, time_bucket). This module tracks each dimension
SEPARATELY (one row for bank=X, another for method=upi) — the schema, not
the prose, is the more concrete contract here.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from recoveryos.config import get_settings
from recoveryos.database import advisory_lock_async, get_app_session_factory
from recoveryos.metrics import systemic_degradation_events_total
from recoveryos.models import AnomalyWindow

# ─── Constants ──────────────────────────────────────────────────────────────
TRAILING_DAYS = 7
SCOPE_COLUMNS = {"bank": "bank", "method": "method", "merchant": "merchant_id"}

SEVERITY_INSUFFICIENT_DATA = "insufficient_data"
SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"

# anomaly_windows.z_score is NUMERIC(6,3) (migrations/0001) — max magnitude
# 999.999. A genuine spike against near-zero historical variance can produce
# an enormous ratio; clamp before writing rather than let Postgres raise a
# numeric field overflow.
Z_SCORE_MAX = 999.999

# Numerical-stability guard for z = diff / std, NOT a change to the formula:
# a real deviation against near-zero historical variance IS correctly a
# very-high-confidence anomaly (small denominator -> large z), so this floors
# the denominator rather than special-casing std_dev == 0 away into "no
# anomaly" or "undefined".
STD_DEV_FLOOR = 1e-4

# Deterministic namespace for cohort-id derivation (arbitrary but fixed —
# changing it would silently mint different cohort ids for the same window
# on every process restart, breaking cross-process agreement).
_COHORT_NAMESPACE = uuid.UUID("6f1e6b1a-9b3e-4b7a-8f2e-9c9a1e3d5b7c")


@dataclass(frozen=True)
class AnomalyResult:
    scope_type: str
    scope_entity: str
    time_bucket: datetime
    baseline_rate: float | None
    observed_rate: float | None
    z_score: float | None
    severity: str
    is_anomaly: bool
    sample_size: int
    cohort_id: str | None


@dataclass(frozen=True)
class SuppressionInfo:
    """What services/diagnosis_engine (and, later, Phase 5's policy engine)
    need to know about an active systemic-suppression cohort."""

    scope_type: str
    scope_entity: str
    time_bucket: datetime
    z_score: float
    cohort_id: str


def floor_to_bucket(ts: datetime, bucket_minutes: int) -> datetime:
    """Floor a timestamp to its aligned N-minute bucket boundary (TRD §3.2).
    Matches simulator/core/clock.py:SimClock.get_15m_bucket()'s semantics,
    generalized to any bucket size and any datetime (not tied to sim clock
    state)."""
    floored_minute = (ts.minute // bucket_minutes) * bucket_minutes
    return ts.replace(minute=floored_minute, second=0, microsecond=0)


def derive_cohort_id(scope_type: str, scope_entity: str, time_bucket: datetime) -> str:
    """Deterministic (uuid5, not uuid4) cohort id for one anomaly window —
    see module docstring for why this exists instead of a stored column."""
    name = f"{scope_type}:{scope_entity}:{time_bucket.isoformat()}"
    return str(uuid.uuid5(_COHORT_NAMESPACE, name))


async def _bucket_stats(
    session: AsyncSession,
    scope_type: str,
    scope_entity: str,
    bucket_start: datetime,
    bucket_minutes: int,
) -> tuple[int, int]:
    """(total, failed) payment counts for one scope within one bucket.

    Buckets on `created_at`, not `failed_at`: `failed_at` is NULL for
    successful payments (recoveryos/models.py), so it can't represent "how
    many attempts happened" — only "how many of them failed, and when".
    `created_at` is populated for every payment regardless of outcome and is
    what the simulator sets to the transaction timestamp either way.
    """
    column = SCOPE_COLUMNS[scope_type]
    bucket_end = bucket_start + timedelta(minutes=bucket_minutes)
    result = await session.execute(
        text(
            f"""
            SELECT count(*) AS total, count(*) FILTER (WHERE status = 'failed') AS failed
            FROM payments
            WHERE {column} = :scope_entity
              AND created_at >= :bucket_start
              AND created_at < :bucket_end
            """
        ),
        {"scope_entity": scope_entity, "bucket_start": bucket_start, "bucket_end": bucket_end},
    )
    row = result.one()
    return int(row.total), int(row.failed)


async def _distinct_entities(
    session: AsyncSession, scope_type: str, bucket_start: datetime, bucket_minutes: int
) -> list[str]:
    column = SCOPE_COLUMNS[scope_type]
    bucket_end = bucket_start + timedelta(minutes=bucket_minutes)
    result = await session.execute(
        text(
            f"""
            SELECT DISTINCT {column}
            FROM payments
            WHERE created_at >= :bucket_start AND created_at < :bucket_end
              AND {column} IS NOT NULL
            """
        ),
        {"bucket_start": bucket_start, "bucket_end": bucket_end},
    )
    return [row[0] for row in result.fetchall()]


async def compute_anomaly_window(
    session: AsyncSession,
    scope_type: str,
    scope_entity: str,
    bucket_start: datetime,
    *,
    bucket_minutes: int | None = None,
    trailing_days: int = TRAILING_DAYS,
    min_sample_size: int | None = None,
    high_threshold: float | None = None,
    medium_threshold: float | None = None,
) -> AnomalyResult:
    """
    Compute (but do not persist) the anomaly window for one
    (scope_type, scope_entity, bucket). Pure computation over real DB reads —
    persistence is a separate step (persist_anomaly_window) so callers can
    inspect a result before deciding to write it (useful in tests).
    """
    if scope_type not in SCOPE_COLUMNS:
        raise ValueError(f"unknown scope_type {scope_type!r}, must be one of {list(SCOPE_COLUMNS)}")

    settings = get_settings()
    bucket_minutes = (
        bucket_minutes if bucket_minutes is not None else settings.anomaly_bucket_minutes
    )
    min_sample_size = (
        min_sample_size if min_sample_size is not None else settings.anomaly_min_sample_size
    )
    high_threshold = (
        high_threshold if high_threshold is not None else settings.anomaly_z_score_high_threshold
    )
    medium_threshold = (
        medium_threshold
        if medium_threshold is not None
        else settings.anomaly_z_score_medium_threshold
    )

    bucket_start = floor_to_bucket(bucket_start, bucket_minutes)
    total, failed = await _bucket_stats(
        session, scope_type, scope_entity, bucket_start, bucket_minutes
    )

    # Minimum sample size guard (TRD §3.2): n < 30 in the CURRENT bucket ->
    # skip z-score entirely, regardless of how much baseline history exists.
    if total < min_sample_size:
        return AnomalyResult(
            scope_type=scope_type,
            scope_entity=scope_entity,
            time_bucket=bucket_start,
            baseline_rate=None,
            observed_rate=(failed / total) if total else None,
            z_score=None,
            severity=SEVERITY_INSUFFICIENT_DATA,
            is_anomaly=False,
            sample_size=total,
            cohort_id=None,
        )

    observed_rate = failed / total

    historical_rates: list[float] = []
    for days_ago in range(1, trailing_days + 1):
        hist_start = bucket_start - timedelta(days=days_ago)
        hist_total, hist_failed = await _bucket_stats(
            session, scope_type, scope_entity, hist_start, bucket_minutes
        )
        if hist_total > 0:
            historical_rates.append(hist_failed / hist_total)

    # Same "can't trust this number" principle as the n<30 guard, applied to
    # baseline history instead of current-bucket volume: a std dev needs at
    # least 2 samples, and 1 sample can't distinguish "always spikes here"
    # from "spiked once, coincidentally".
    if len(historical_rates) < 2:
        return AnomalyResult(
            scope_type=scope_type,
            scope_entity=scope_entity,
            time_bucket=bucket_start,
            baseline_rate=None,
            observed_rate=observed_rate,
            z_score=None,
            severity=SEVERITY_INSUFFICIENT_DATA,
            is_anomaly=False,
            sample_size=total,
            cohort_id=None,
        )

    baseline_rate = statistics.mean(historical_rates)
    baseline_std_dev = statistics.stdev(historical_rates)
    safe_std_dev = max(baseline_std_dev, STD_DEV_FLOOR)

    z = (observed_rate - baseline_rate) / safe_std_dev
    z_clamped = max(min(z, Z_SCORE_MAX), -Z_SCORE_MAX)

    if z_clamped > high_threshold:
        severity = SEVERITY_HIGH
    elif z_clamped > medium_threshold:
        severity = SEVERITY_MEDIUM
    else:
        severity = SEVERITY_LOW

    # "flagged" (medium) is still an anomaly for dashboard/alert purposes —
    # only "high" additionally forms a cohort and suppresses retries.
    is_anomaly = severity in (SEVERITY_MEDIUM, SEVERITY_HIGH)
    cohort_id = (
        derive_cohort_id(scope_type, scope_entity, bucket_start)
        if severity == SEVERITY_HIGH
        else None
    )

    return AnomalyResult(
        scope_type=scope_type,
        scope_entity=scope_entity,
        time_bucket=bucket_start,
        baseline_rate=baseline_rate,
        observed_rate=observed_rate,
        z_score=z_clamped,
        severity=severity,
        is_anomaly=is_anomaly,
        sample_size=total,
        cohort_id=cohort_id,
    )


async def persist_anomaly_window(app_session: AsyncSession, result: AnomalyResult) -> None:
    """
    Upsert into anomaly_windows, keyed on the UNIQUE(scope_type, scope_entity,
    time_bucket) index — re-running detection for the same bucket updates it
    in place rather than erroring or duplicating.

    MUST run on an app_role session — diagnoser_role has SELECT only on this
    table (migrations/versions/0002_db_roles.py), confirmed by
    test_diagnoser_role_has_no_write_access.

    TRD §10's systemic_degradation_events_total{bank} counts EVENTS
    (transitions into a high-severity, is_anomaly=true state), not every
    re-computation of the same bucket -- a bucket can legitimately be
    re-detected many times as more payments land in it before the bucket
    closes, and counting each recomputation would inflate the metric by
    however many times detection happened to run, not by how many real
    incidents occurred. So the PRIOR severity for this exact
    (scope_type, scope_entity, time_bucket) is read before the upsert, and
    the counter only increments on a genuine not-high -> high transition.

    Production Architecture Domain Audit finding #5: the read-then-write
    pair above was a real TOCTOU -- two concurrent callers computing the
    SAME (scope_type, scope_entity, time_bucket) window (plausible once
    F3's horizontal-replica wiring lets multiple pipeline_orchestrator
    instances run, or apps/api/routers/simulate.py's demo anomaly
    injection races a real detection cycle) could both read "not
    previously high" before either commits, and both increment the
    counter for what is really ONE transition. Fixed the same way
    services/pipeline/reconciliation.py's equivalent race was fixed
    (Payments Domain Audit finding #5): the entire read-check-write-
    increment sequence is now held inside a single Postgres advisory
    lock, keyed on this exact (scope_type, scope_entity, time_bucket)
    tuple -- not a new mechanism, the same recoveryos.database.
    advisory_lock_async primitive.
    """
    lock_key = (
        f"anomaly-window:{result.scope_type}:{result.scope_entity}:{result.time_bucket.isoformat()}"
    )
    async with advisory_lock_async(app_session, key=lock_key):
        previous = (
            await app_session.execute(
                text(
                    "SELECT severity FROM anomaly_windows "
                    "WHERE scope_type = :scope_type AND scope_entity = :scope_entity "
                    "AND time_bucket = :time_bucket"
                ),
                {
                    "scope_type": result.scope_type,
                    "scope_entity": result.scope_entity,
                    "time_bucket": result.time_bucket,
                },
            )
        ).first()
        previously_high = previous is not None and previous[0] == SEVERITY_HIGH

        stmt = (
            pg_insert(AnomalyWindow)
            .values(
                window_id=str(uuid.uuid4()),
                scope_type=result.scope_type,
                scope_entity=result.scope_entity,
                time_bucket=result.time_bucket,
                baseline_rate=result.baseline_rate,
                observed_rate=result.observed_rate,
                z_score=result.z_score,
                severity=result.severity,
                is_anomaly=result.is_anomaly,
            )
            .on_conflict_do_update(
                index_elements=["scope_type", "scope_entity", "time_bucket"],
                set_={
                    "baseline_rate": result.baseline_rate,
                    "observed_rate": result.observed_rate,
                    "z_score": result.z_score,
                    "severity": result.severity,
                    "is_anomaly": result.is_anomaly,
                },
            )
        )
        await app_session.execute(stmt)
        await app_session.commit()

        if (
            result.scope_type == "bank"
            and result.severity == SEVERITY_HIGH
            and result.is_anomaly
            and not previously_high
        ):
            systemic_degradation_events_total.labels(bank=result.scope_entity).inc()


async def run_anomaly_detection(
    bucket_start: datetime,
    scope_types: tuple[str, ...] = ("bank", "method"),
    session: AsyncSession | None = None,
) -> list[AnomalyResult]:
    """
    Entry point for one detection pass over ALL entities in one bucket:
    discovers every (scope_type, scope_entity) combination with traffic in
    the bucket, computes and persists an anomaly window for each. This is
    the proactive/sweep counterpart to the reactive path production
    actually uses today — services/pipeline/consumer.py calls
    compute_anomaly_window/persist_anomaly_window directly, per payment, as
    each failure arrives, and never goes through this bucket-wide sweep.

    Unused today — no scheduler, no caller, no test exercises this
    function specifically (only its two building blocks, via the reactive
    path above). Kept as the batch entry point a future periodic sweep
    (catching entities with no fresh failures to react to, e.g. a bank
    that's been quietly degraded with low volume) would call; wiring that
    scheduler is a deployment concern for a later phase.
    Pass `session` to reuse an existing app_role session (e.g. in tests);
    otherwise a fresh one is opened and closed here.
    """
    results: list[AnomalyResult] = []

    async def _run(s: AsyncSession) -> None:
        for scope_type in scope_types:
            entities = await _distinct_entities(
                s, scope_type, bucket_start, get_settings().anomaly_bucket_minutes
            )
            for entity in entities:
                result = await compute_anomaly_window(s, scope_type, entity, bucket_start)
                await persist_anomaly_window(s, result)
                results.append(result)

    if session is not None:
        await _run(session)
    else:
        async with get_app_session_factory()() as owned_session:
            await _run(owned_session)

    return results


async def is_cohort_suppressed(
    session: AsyncSession,
    *,
    bank: str | None = None,
    method: str | None = None,
    as_of: datetime | None = None,
    suppression_window_minutes: int = 30,
) -> SuppressionInfo | None:
    """
    Phase 5 hook: is there an ACTIVE high-severity systemic anomaly currently
    suppressing RETRY_NOW for this payment's bank and/or method? Returns the
    matching window info (with its derived cohort_id) if so, else None.

    "Active" = a high-severity window whose bucket falls within
    `suppression_window_minutes` of `as_of` — the re-evaluation window TRD
    §1.4/§3.2 refers to ("suppress individual retries for cohort until
    re-evaluation window passes"). Checks bank first, then method; either
    scope being actively degraded is enough to suppress. This function only
    READS anomaly_windows — it makes no policy decision itself (that's
    Phase 5's SystemicSuppressionRule, which this exists to feed).
    """
    as_of = as_of or datetime.now(UTC)
    cutoff = as_of - timedelta(minutes=suppression_window_minutes)

    for scope_type, scope_entity in (("bank", bank), ("method", method)):
        if not scope_entity:
            continue
        result = await session.execute(
            text(
                """
                SELECT scope_type, scope_entity, time_bucket, z_score
                FROM anomaly_windows
                WHERE scope_type = :scope_type AND scope_entity = :scope_entity
                  AND severity = 'high' AND time_bucket >= :cutoff
                ORDER BY time_bucket DESC
                LIMIT 1
                """
            ),
            {"scope_type": scope_type, "scope_entity": scope_entity, "cutoff": cutoff},
        )
        row = result.first()
        if row is not None:
            return SuppressionInfo(
                scope_type=row.scope_type,
                scope_entity=row.scope_entity,
                time_bucket=row.time_bucket,
                z_score=float(row.z_score),
                cohort_id=derive_cohort_id(row.scope_type, row.scope_entity, row.time_bucket),
            )
    return None
