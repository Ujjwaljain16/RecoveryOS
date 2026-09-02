"""
Phase 12 -- Recovery Mission: an explicit, code-owned state machine wrapping
one payment's full investigate -> decide -> execute -> observe lifecycle,
however many rounds it takes to reach a terminal state.

Governing principle (Phase 12 design doc): **the state machine is code-
owned, never LLM-owned.** The AI (investigator, Phase 11 recommendation) is
invoked because a mission is already in a given state (e.g. INVESTIGATING);
it never announces or causes a transition itself -- only the functions in
this module ever write recovery_missions.state, and every write is checked
against ALLOWED_TRANSITIONS first. A mission's budget fields
(max_investigation_rounds/max_attempts/max_mission_duration_seconds) are
set once at creation from recoveryos.config.Settings and are read-only from
that point on -- check_budget() is a pure function of the mission's own
row, nothing an LLM output can influence.

Two call sites, same async/sync split as services/pipeline/ledger.py, for
the same reason: services/pipeline/consumer.py (async) drives missions
through OBSERVED -> INVESTIGATING -> PLANNING -> AWAITING_AUTHORIZATION,
workers/execution_worker.py (sync) drives them through
EXECUTING -> OBSERVING_OUTCOME -> {RECOVERED | ESCALATED | TERMINATED} (or
back to OBSERVING_OUTCOME awaiting a scheduled re-evaluation, Phase 13).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

TERMINAL_STATES = frozenset({"RECOVERED", "ESCALATED", "TERMINATED"})

# Code-owned transition table -- the ONLY place that decides what state can
# follow what. transition_mission_async/_sync below refuse (raise) any
# transition not listed here, regardless of what caller code requests.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "OBSERVED": frozenset({"INVESTIGATING"}),
    "INVESTIGATING": frozenset({"PLANNING", "TERMINATED"}),
    "PLANNING": frozenset({"AWAITING_AUTHORIZATION", "TERMINATED"}),
    "AWAITING_AUTHORIZATION": frozenset({"EXECUTING", "ESCALATED", "TERMINATED"}),
    "EXECUTING": frozenset({"OBSERVING_OUTCOME"}),
    # OBSERVING_OUTCOME -> INVESTIGATING is Phase 13's closed loop: a
    # deferred RETRY_LATER window elapsing (existing, Task REPLAN1) and a
    # FAILED immediate attempt with budget remaining (new) both resolve
    # through this exact same transition.
    "OBSERVING_OUTCOME": frozenset({"RECOVERED", "ESCALATED", "TERMINATED", "INVESTIGATING"}),
    "RECOVERED": frozenset(),
    "ESCALATED": frozenset(),
    "TERMINATED": frozenset(),
}


def validate_transition(from_state: str, to_state: str) -> bool:
    """Pure. The single source of truth for "is this transition legal" --
    hand-testable with no DB, and the one thing both the async and sync
    writers below defer to before touching a row."""
    return to_state in ALLOWED_TRANSITIONS.get(from_state, frozenset())


@dataclass(frozen=True)
class BudgetStatus:
    exhausted: bool
    reason: str | None  # MAX_ATTEMPTS_EXCEEDED | MAX_ROUNDS_EXCEEDED | MISSION_DURATION_EXCEEDED


def check_budget(
    *,
    current_round: int,
    max_investigation_rounds: int,
    current_attempt: int,
    max_attempts: int,
    started_at: datetime,
    max_mission_duration_seconds: int,
    now: datetime,
) -> BudgetStatus:
    """
    Pure. The sole authority on "has this mission's hard envelope run out" --
    a mission's own row is the only input, never anything an LLM produced.
    Checked in a fixed priority order (rounds, then attempts, then time) so
    the reported reason is deterministic when more than one limit is
    simultaneously exceeded.
    """
    if current_round > max_investigation_rounds:
        return BudgetStatus(True, "MAX_ROUNDS_EXCEEDED")
    if current_attempt > max_attempts:
        return BudgetStatus(True, "MAX_ATTEMPTS_EXCEEDED")
    if now - started_at > timedelta(seconds=max_mission_duration_seconds):
        return BudgetStatus(True, "MISSION_DURATION_EXCEEDED")
    return BudgetStatus(False, None)


class InvalidMissionTransitionError(Exception):
    pass


_MISSION_COLUMNS = (
    "mission_id, payment_id, state, objective, max_investigation_rounds, "
    "max_attempts, max_mission_duration_seconds, max_money_exposure_paise, "
    "current_round, current_attempt, started_at, expires_at, ended_at"
)


async def find_mission_for_payment_async(session, payment_id: str) -> dict | None:
    """
    Read-only lookup, ANY state (unlike get_or_create_mission_async's own
    internal lookup, which deliberately excludes terminal missions -- that
    exclusion is correct for "is there an ACTIVE mission to reinvestigate,"
    wrong here). For workers/execution_worker.py: a job reaching
    process_job() should always already have a mission (services/pipeline/
    consumer.py creates it before ever enqueueing), possibly already
    terminal (a genuinely redelivered job for an already-completed mission,
    Redis's normal at-least-once guarantee) -- never a reason to create a
    NEW one. See find_mission_for_payment_sync's docstring for the full
    reasoning; this is its async mirror, used nowhere in this codebase
    today but kept alongside it for symmetry/future async execution paths.
    """
    from sqlalchemy import text

    row = (
        (
            await session.execute(
                text(
                    f"SELECT {_MISSION_COLUMNS} FROM recovery_missions "
                    "WHERE payment_id = :pid ORDER BY created_at DESC LIMIT 1"
                ),
                {"pid": payment_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def find_mission_for_payment_sync(conn, payment_id: str) -> dict | None:
    """
    Sync mirror of find_mission_for_payment_async -- the one
    workers/execution_worker.py actually uses. Deliberately NOT get-or-
    create: execution_worker never originates a mission, only reacts to a
    job services/pipeline/consumer.py already decided to run, so the
    mission should already exist. Using get_or_create_mission_sync here
    (as an earlier version of this code did) meant a genuinely redelivered
    job (Redis's normal at-least-once guarantee) landing AFTER its mission
    had already reached a terminal state (RECOVERED/ESCALATED/TERMINATED)
    would spuriously spawn a second, orphaned OBSERVED mission for that
    payment -- get_or_create_mission_sync's own "state NOT IN (terminal)"
    filter correctly excludes it from being found, then creates a new one
    since none is "active." This function has no such filter and never
    creates anything -- a redelivery reaching here just finds the (possibly
    already-terminal) real mission, and process_job's own mission_trackable
    guard (state != EXECUTING) already skips re-advancing it either way.
    """
    from sqlalchemy import text

    row = (
        conn.execute(
            text(
                f"SELECT {_MISSION_COLUMNS} FROM recovery_missions "
                "WHERE payment_id = :pid ORDER BY created_at DESC LIMIT 1"
            ),
            {"pid": payment_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


# ─── Async writer (services/pipeline/consumer.py) ──────────────────────────


async def get_or_create_mission_async(
    session,
    *,
    payment_id: str,
    amount_paise: int,
    now: datetime,
    max_investigation_rounds: int,
    max_attempts: int,
    max_mission_duration_seconds: int,
) -> tuple[dict, bool]:
    """
    Returns (mission_row_as_dict, was_created). Looks up the payment's
    currently ACTIVE mission (state NOT IN the terminal set -- the same
    condition migration 0022's partial unique index enforces) first; only
    creates a new one if none exists. A payment can accumulate multiple
    TERMINAL missions over its lifetime (escalated once, a brand new
    failure later starts a fresh mission) but never two concurrently
    active ones -- the partial unique index is the physical backstop this
    lookup-then-insert relies on.
    """
    from sqlalchemy import text

    existing = (
        (
            await session.execute(
                text(
                    "SELECT mission_id, payment_id, state, objective, max_investigation_rounds, "
                    "max_attempts, max_mission_duration_seconds, max_money_exposure_paise, "
                    "current_round, current_attempt, started_at, expires_at, ended_at "
                    "FROM recovery_missions WHERE payment_id = :pid "
                    "AND state NOT IN ('RECOVERED','ESCALATED','TERMINATED') "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"pid": payment_id},
            )
        )
        .mappings()
        .first()
    )
    if existing is not None:
        return dict(existing), False

    mission_id = str(uuid.uuid4())
    objective = (
        "maximize expected recovered revenue subject to deterministic "
        "safety, policy, and budget constraints"
    )
    expires_at = now + timedelta(seconds=max_mission_duration_seconds)
    # ON CONFLICT DO NOTHING against migration 0022's own partial unique
    # index (uq_recovery_missions_one_active_per_payment) -- two callers
    # racing on the SAME payment_id (e.g. two worker threads processing the
    # same enqueued job concurrently, the exact scenario
    # tests/integration/test_execution_worker.py's advisory-lock tests
    # exercise) can both pass the SELECT-based lookup above and both reach
    # this INSERT; only one wins the unique index, the loser re-selects the
    # winner's row below instead of raising IntegrityError. Same race-safe
    # pattern as persist_diagnosis/persist_decision/schedule_reevaluation
    # elsewhere in this codebase.
    row = (
        (
            await session.execute(
                text(
                    "INSERT INTO recovery_missions "
                    "(mission_id, payment_id, state, objective, max_investigation_rounds, "
                    "max_attempts, max_mission_duration_seconds, max_money_exposure_paise, "
                    "current_round, current_attempt, started_at, expires_at) "
                    "VALUES (:mid, :pid, 'OBSERVED', :objective, :max_rounds, :max_attempts, "
                    ":max_duration, :max_exposure, 0, 0, :started_at, :expires_at) "
                    "ON CONFLICT (payment_id) WHERE state NOT IN "
                    "('RECOVERED','ESCALATED','TERMINATED') DO NOTHING "
                    "RETURNING mission_id, payment_id, state, objective, max_investigation_rounds, "
                    "max_attempts, max_mission_duration_seconds, max_money_exposure_paise, "
                    "current_round, current_attempt, started_at, expires_at, ended_at"
                ),
                {
                    "mid": mission_id,
                    "pid": payment_id,
                    "objective": objective,
                    "max_rounds": max_investigation_rounds,
                    "max_attempts": max_attempts,
                    "max_duration": max_mission_duration_seconds,
                    "max_exposure": amount_paise,
                    "started_at": now,
                    "expires_at": expires_at,
                },
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        await session.commit()
        winner = (
            (
                await session.execute(
                    text(
                        "SELECT mission_id, payment_id, state, objective, max_investigation_rounds, "
                        "max_attempts, max_mission_duration_seconds, max_money_exposure_paise, "
                        "current_round, current_attempt, started_at, expires_at, ended_at "
                        "FROM recovery_missions WHERE payment_id = :pid "
                        "AND state NOT IN ('RECOVERED','ESCALATED','TERMINATED') "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"pid": payment_id},
                )
            )
            .mappings()
            .first()
        )
        return dict(winner), False
    await session.commit()
    return dict(row), True


async def transition_mission_async(
    session,
    *,
    mission_id: str,
    to_state: str,
    event_type: str,
    actor: str,
    payload: dict,
    increment_round: bool = False,
    increment_attempt: bool = False,
    now: datetime,
) -> dict:
    """
    The ONLY function that mutates recovery_missions.state (async side).
    Locks the mission row (FOR UPDATE) so the transition-validate + next-
    sequence_number computation + both writes happen atomically against
    concurrent appenders -- in practice one payment's mission is processed
    by one pipeline stage at a time, but this makes that a guarantee, not
    an assumption. Raises InvalidMissionTransitionError rather than silently
    applying an illegal transition -- validate_transition() is the single
    source of truth, checked here against the row's REAL current state,
    not whatever the caller believes it to be.
    """
    from sqlalchemy import text

    row = (
        (
            await session.execute(
                text("SELECT state FROM recovery_missions WHERE mission_id = :mid FOR UPDATE"),
                {"mid": mission_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise InvalidMissionTransitionError(f"mission_id={mission_id} does not exist")
    from_state = row["state"]
    if not validate_transition(from_state, to_state):
        raise InvalidMissionTransitionError(
            f"mission_id={mission_id}: {from_state} -> {to_state} is not an allowed transition"
        )

    ended_at = now if to_state in TERMINAL_STATES else None
    updated = (
        (
            await session.execute(
                text(
                    "UPDATE recovery_missions SET state = :to_state, updated_at = :now, "
                    "current_round = current_round + :inc_round, "
                    "current_attempt = current_attempt + :inc_attempt, "
                    "ended_at = COALESCE(:ended_at, ended_at) "
                    "WHERE mission_id = :mid "
                    "RETURNING mission_id, payment_id, state, objective, max_investigation_rounds, "
                    "max_attempts, max_mission_duration_seconds, max_money_exposure_paise, "
                    "current_round, current_attempt, started_at, expires_at, ended_at"
                ),
                {
                    "to_state": to_state,
                    "now": now,
                    "inc_round": 1 if increment_round else 0,
                    "inc_attempt": 1 if increment_attempt else 0,
                    "ended_at": ended_at,
                    "mid": mission_id,
                },
            )
        )
        .mappings()
        .first()
    )

    next_seq = (
        await session.execute(
            text(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM mission_events "
                "WHERE mission_id = :mid"
            ),
            {"mid": mission_id},
        )
    ).scalar_one()
    await session.execute(
        text(
            "INSERT INTO mission_events "
            "(event_id, mission_id, sequence_number, state, event_type, actor, payload) "
            "VALUES (:eid, :mid, :seq, :state, :event_type, :actor, :payload)"
        ),
        {
            "eid": str(uuid.uuid4()),
            "mid": mission_id,
            "seq": next_seq,
            "state": to_state,
            "event_type": event_type,
            "actor": actor,
            "payload": _json_dumps(payload),
        },
    )
    await session.commit()
    return dict(updated)


async def log_mission_event_async(
    session, *, mission_id: str, event_type: str, actor: str, payload: dict
) -> None:
    """
    Appends a mission_events row WITHOUT changing recovery_missions.state --
    for narration points that happen WITHIN a state, not a transition
    between two (e.g. AI_RECOMMENDATION/HYPOTHESIS_UPDATED happen while the
    mission is still INVESTIGATING; the transition to PLANNING is its own
    separate transition_mission_async call once the round concludes). Same
    row-lock discipline as transition_mission_async, for the same reason:
    sequence_number must stay gap-free and ordered under concurrent
    appenders.
    """
    from sqlalchemy import text

    row = (
        (
            await session.execute(
                text("SELECT state FROM recovery_missions WHERE mission_id = :mid FOR UPDATE"),
                {"mid": mission_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise InvalidMissionTransitionError(f"mission_id={mission_id} does not exist")

    next_seq = (
        await session.execute(
            text(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM mission_events "
                "WHERE mission_id = :mid"
            ),
            {"mid": mission_id},
        )
    ).scalar_one()
    await session.execute(
        text(
            "INSERT INTO mission_events "
            "(event_id, mission_id, sequence_number, state, event_type, actor, payload) "
            "VALUES (:eid, :mid, :seq, :state, :event_type, :actor, :payload)"
        ),
        {
            "eid": str(uuid.uuid4()),
            "mid": mission_id,
            "seq": next_seq,
            "state": row["state"],
            "event_type": event_type,
            "actor": actor,
            "payload": _json_dumps(payload),
        },
    )
    await session.commit()


def _json_dumps(payload: dict) -> str:
    import json

    return json.dumps(payload, default=str)


# ─── Sync writer (workers/execution_worker.py) ─────────────────────────────


def get_or_create_mission_sync(
    conn,
    *,
    payment_id: str,
    amount_paise: int,
    now: datetime,
    max_investigation_rounds: int,
    max_attempts: int,
    max_mission_duration_seconds: int,
) -> tuple[dict, bool]:
    """Sync mirror of get_or_create_mission_async -- see its docstring.
    In practice, execution_worker.py only ever REUSES a mission
    services/pipeline/consumer.py already created (a job only reaches
    execution_worker after a decision was made), but this stays
    lookup-then-create rather than lookup-or-raise for the same reason
    every other role boundary in this codebase fails safe instead of
    crashing: a direct test/manual invocation without a prior consumer.py
    pass must not blow up."""
    from sqlalchemy import text

    existing = (
        conn.execute(
            text(
                "SELECT mission_id, payment_id, state, objective, max_investigation_rounds, "
                "max_attempts, max_mission_duration_seconds, max_money_exposure_paise, "
                "current_round, current_attempt, started_at, expires_at, ended_at "
                "FROM recovery_missions WHERE payment_id = :pid "
                "AND state NOT IN ('RECOVERED','ESCALATED','TERMINATED') "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"pid": payment_id},
        )
        .mappings()
        .first()
    )
    if existing is not None:
        return dict(existing), False

    mission_id = str(uuid.uuid4())
    objective = (
        "maximize expected recovered revenue subject to deterministic "
        "safety, policy, and budget constraints"
    )
    expires_at = now + timedelta(seconds=max_mission_duration_seconds)
    # Same race-safe ON CONFLICT DO NOTHING as get_or_create_mission_async --
    # see that function's comment. Sync side matters even more here: two
    # execution_worker.py threads/processes calling process_job() for the
    # SAME payment concurrently is exactly what
    # tests/integration/test_execution_worker.py's advisory-lock tests
    # exercise for real.
    row = (
        conn.execute(
            text(
                "INSERT INTO recovery_missions "
                "(mission_id, payment_id, state, objective, max_investigation_rounds, "
                "max_attempts, max_mission_duration_seconds, max_money_exposure_paise, "
                "current_round, current_attempt, started_at, expires_at) "
                "VALUES (:mid, :pid, 'OBSERVED', :objective, :max_rounds, :max_attempts, "
                ":max_duration, :max_exposure, 0, 0, :started_at, :expires_at) "
                "ON CONFLICT (payment_id) WHERE state NOT IN "
                "('RECOVERED','ESCALATED','TERMINATED') DO NOTHING "
                "RETURNING mission_id, payment_id, state, objective, max_investigation_rounds, "
                "max_attempts, max_mission_duration_seconds, max_money_exposure_paise, "
                "current_round, current_attempt, started_at, expires_at, ended_at"
            ),
            {
                "mid": mission_id,
                "pid": payment_id,
                "objective": objective,
                "max_rounds": max_investigation_rounds,
                "max_attempts": max_attempts,
                "max_duration": max_mission_duration_seconds,
                "max_exposure": amount_paise,
                "started_at": now,
                "expires_at": expires_at,
            },
        )
        .mappings()
        .first()
    )
    if row is None:
        conn.commit()
        winner = (
            conn.execute(
                text(
                    "SELECT mission_id, payment_id, state, objective, max_investigation_rounds, "
                    "max_attempts, max_mission_duration_seconds, max_money_exposure_paise, "
                    "current_round, current_attempt, started_at, expires_at, ended_at "
                    "FROM recovery_missions WHERE payment_id = :pid "
                    "AND state NOT IN ('RECOVERED','ESCALATED','TERMINATED') "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"pid": payment_id},
            )
            .mappings()
            .first()
        )
        return dict(winner), False
    conn.commit()
    return dict(row), True


def transition_mission_sync(
    conn,
    *,
    mission_id: str,
    to_state: str,
    event_type: str,
    actor: str,
    payload: dict,
    increment_round: bool = False,
    increment_attempt: bool = False,
    now: datetime,
) -> dict:
    """Sync mirror of transition_mission_async -- see its docstring for the
    full locking/validation discipline, identical here."""
    from sqlalchemy import text

    row = (
        conn.execute(
            text("SELECT state FROM recovery_missions WHERE mission_id = :mid FOR UPDATE"),
            {"mid": mission_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise InvalidMissionTransitionError(f"mission_id={mission_id} does not exist")
    from_state = row["state"]
    if not validate_transition(from_state, to_state):
        raise InvalidMissionTransitionError(
            f"mission_id={mission_id}: {from_state} -> {to_state} is not an allowed transition"
        )

    ended_at = now if to_state in TERMINAL_STATES else None
    updated = (
        conn.execute(
            text(
                "UPDATE recovery_missions SET state = :to_state, updated_at = :now, "
                "current_round = current_round + :inc_round, "
                "current_attempt = current_attempt + :inc_attempt, "
                "ended_at = COALESCE(:ended_at, ended_at) "
                "WHERE mission_id = :mid "
                "RETURNING mission_id, payment_id, state, objective, max_investigation_rounds, "
                "max_attempts, max_mission_duration_seconds, max_money_exposure_paise, "
                "current_round, current_attempt, started_at, expires_at, ended_at"
            ),
            {
                "to_state": to_state,
                "now": now,
                "inc_round": 1 if increment_round else 0,
                "inc_attempt": 1 if increment_attempt else 0,
                "ended_at": ended_at,
                "mid": mission_id,
            },
        )
        .mappings()
        .first()
    )

    next_seq = conn.execute(
        text(
            "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM mission_events WHERE mission_id = :mid"
        ),
        {"mid": mission_id},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO mission_events "
            "(event_id, mission_id, sequence_number, state, event_type, actor, payload) "
            "VALUES (:eid, :mid, :seq, :state, :event_type, :actor, :payload)"
        ),
        {
            "eid": str(uuid.uuid4()),
            "mid": mission_id,
            "seq": next_seq,
            "state": to_state,
            "event_type": event_type,
            "actor": actor,
            "payload": _json_dumps(payload),
        },
    )
    conn.commit()
    return dict(updated)


def log_mission_event_sync(conn, *, mission_id: str, event_type: str, actor: str, payload: dict) -> None:
    """Sync mirror of log_mission_event_async -- see its docstring."""
    from sqlalchemy import text

    row = (
        conn.execute(
            text("SELECT state FROM recovery_missions WHERE mission_id = :mid FOR UPDATE"),
            {"mid": mission_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise InvalidMissionTransitionError(f"mission_id={mission_id} does not exist")

    next_seq = conn.execute(
        text(
            "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM mission_events WHERE mission_id = :mid"
        ),
        {"mid": mission_id},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO mission_events "
            "(event_id, mission_id, sequence_number, state, event_type, actor, payload) "
            "VALUES (:eid, :mid, :seq, :state, :event_type, :actor, :payload)"
        ),
        {
            "eid": str(uuid.uuid4()),
            "mid": mission_id,
            "seq": next_seq,
            "state": row["state"],
            "event_type": event_type,
            "actor": actor,
            "payload": _json_dumps(payload),
        },
    )
    conn.commit()
