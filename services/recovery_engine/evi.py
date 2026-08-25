"""
Expected Recovery Value (EVI) — TRD §3.1, gaps.md §A.2/§B.4.

    EVI(payment, action) =
        P(recover | payment, action) x amount
        - cost(action)
        - friction_penalty(action, customer)
        - risk_penalty(action, context)

Two hardening decisions carried over verbatim from gaps.md, both non-negotiable:

1. INTEGER ARITHMETIC ONLY (gaps.md §B.4). recovery_prob_bps is basis points
   (0-10000; 82.00% = 8200), never a float. Every money value stays BIGINT
   paise end to end. No float literal or float() cast appears anywhere in
   this file — test_evi_calculation_uses_only_integer_arithmetic AST-checks
   this file specifically for that.

2. ACTION COST FROM A DB TABLE, NOT A HARDCODED CONSTANT (gaps.md §A.2).
   get_action_cost() resolves merchant-specific cost first, platform default
   (merchant_id IS NULL) second — see action_costs table, migrations/0001.
   This is the one function in this module that does I/O; calculate_evi()
   itself is a pure function over already-fetched values, same purity
   discipline as services/policy_engine.

Economic interpretation of "amount" (resolved per this phase's design
discussion, not invented fresh): Phase 2's certified economics
(simulator/episodes/models.py, models/recovery/evaluate.py) both define the
platform's actual recovered value as amount_paise x RECOVERY_MARGIN (15%),
not the full transaction amount — a recovered payment is revenue the
merchant already owns, RecoveryOS only earns its take-rate on it.
RECOVERY_MARGIN_BPS below is that same 15% re-expressed as an integer to
keep this file float-free; kept in sync with simulator/episodes/models.py's
RECOVERY_MARGIN by test_recovery_margin_bps_matches_phase2_constant.

risk_penalty is deliberately NOT a re-expression of timing.py's probability
adjustment (that already lowers P(recover) itself for RETRY_NOW during a
high-severity systemic anomaly — see services/recovery_engine/timing.py's
module docstring for why). risk_penalty is a small, fixed, non-scaled paise
surcharge representing the additional operational/reputational cost of
choosing to retry into a known-unstable bank at all, independent of whether
this specific attempt happens to succeed — implementing TRD §3.1's explicit
requirement that risk_penalty be its own nonzero term during systemic
windows, biasing the argmax further toward RETRY_LATER/DO_NOTHING even in a
borderline case.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from recoveryos.models import ActionCost as ActionCostRow
from services.recovery_engine.timing import AnomalyContext

BPS_SCALE = 10_000

# 15% platform margin on a recovered transaction — matches
# simulator/episodes/models.py:RECOVERY_MARGIN=0.15 and
# models/recovery/evaluate.py's RECOVERY_MARGIN, re-expressed as an integer
# so this file never touches a float. See module docstring.
RECOVERY_MARGIN_BPS = 1_500

# Customer-type friction multiplier — TRD §3.1: "returning customers get
# lower friction penalty for reminders, first-time customers get higher."
# Scoped to REMINDER only (the one action TRD's text names); every other
# action uses friction_base_paise unmodified (multiplier = BPS_SCALE, i.e. 1.0x).
RETURNING_CUSTOMER_REMINDER_FRICTION_BPS = 5_000  # 50% of base
NEW_CUSTOMER_REMINDER_FRICTION_BPS = 15_000  # 150% of base

# Fixed risk surcharge (paise) applied ONLY to RETRY_NOW during an active,
# sufficiently-sampled, HIGH-severity systemic anomaly — see module
# docstring for why this is a separate, non-scaled term from timing.py's
# probability penalty.
SYSTEMIC_RISK_PENALTY_PAISE = 500  # ₹5, a deliberate fixed bias constant

VALID_ACTION_TYPES = frozenset(
    {"RETRY_NOW", "RETRY_LATER", "ALT_ROUTE", "REMINDER", "ESCALATE", "DO_NOTHING"}
)


@dataclass(frozen=True)
class ActionCost:
    """Pure-data view of one action_costs row — what calculate_evi() and
    friction_penalty_paise() actually consume. Built by get_action_cost()
    (the I/O boundary) so the calculation functions stay DB-free."""

    action_type: str
    cost_paise: int
    friction_base_paise: int


async def get_action_cost(
    session: AsyncSession, merchant_id: str | None, action_type: str
) -> ActionCost:
    """
    Resolve action cost: merchant-specific row first, platform default
    (merchant_id IS NULL) second (gaps.md §A.2's exact resolution order).
    Raises if even the platform default is missing — that would mean the
    Phase 0 seed data (migrations/0001) was never applied, a real
    configuration error that should fail loudly, not silently return zeros.
    """
    if merchant_id is not None:
        row = (
            await session.execute(
                select(ActionCostRow).where(
                    ActionCostRow.merchant_id == merchant_id,
                    ActionCostRow.action_type == action_type,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            return ActionCost(
                action_type=row.action_type,
                cost_paise=row.cost_paise,
                friction_base_paise=row.friction_base_paise,
            )

    platform_row = (
        await session.execute(
            select(ActionCostRow).where(
                ActionCostRow.merchant_id.is_(None),
                ActionCostRow.action_type == action_type,
            )
        )
    ).scalar_one_or_none()
    if platform_row is None:
        raise RuntimeError(
            f"No platform-default action_costs row for action_type={action_type!r} — "
            f"migrations/0001's seed data is missing or was never applied."
        )
    return ActionCost(
        action_type=platform_row.action_type,
        cost_paise=platform_row.cost_paise,
        friction_base_paise=platform_row.friction_base_paise,
    )


def friction_penalty_paise(
    action_type: str, friction_base_paise: int, customer_is_returning: bool
) -> int:
    """
    TRD §3.1: friction penalty scaled by customer opt-out risk. Only REMINDER
    is differentiated by customer type (TRD's own text names reminders
    specifically); every other action uses friction_base_paise unmodified.
    """
    if action_type != "REMINDER":
        return friction_base_paise
    multiplier_bps = (
        RETURNING_CUSTOMER_REMINDER_FRICTION_BPS
        if customer_is_returning
        else NEW_CUSTOMER_REMINDER_FRICTION_BPS
    )
    return (friction_base_paise * multiplier_bps) // BPS_SCALE


def risk_penalty_paise(action_type: str, anomaly_context: AnomalyContext | None) -> int:
    """
    TRD §3.1: "risk_penalty: nonzero only for actions during a SYSTEMIC
    anomaly window." Nonzero ONLY for RETRY_NOW, ONLY during an active,
    sufficiently-sampled, high-severity anomaly — see module docstring for
    why this is separate from timing.py's probability adjustment.
    """
    if action_type != "RETRY_NOW" or anomaly_context is None:
        return 0
    if not anomaly_context.has_sufficient_data:
        return 0
    if anomaly_context.severity != "high" or not anomaly_context.is_anomaly:
        return 0
    return SYSTEMIC_RISK_PENALTY_PAISE


def calculate_evi(
    recovery_prob_bps: int,
    amount_paise: int,
    cost_paise: int,
    friction_paise: int,
    risk_paise: int,
) -> int:
    """
    Pure integer-arithmetic EVI, in paise. Can be negative (that's the
    DO_NOTHING trigger — see next_best_action.py). No DB/network calls,
    no float anywhere.

    expected_recovery_paise = amount_paise x recovery_prob_bps x
        RECOVERY_MARGIN_BPS, scaled down by BPS_SCALE twice (once per bps
        factor) — floor division throughout, deterministic.
    """
    expected_recovery_paise = (amount_paise * recovery_prob_bps * RECOVERY_MARGIN_BPS) // (
        BPS_SCALE * BPS_SCALE
    )
    return expected_recovery_paise - cost_paise - friction_paise - risk_paise
