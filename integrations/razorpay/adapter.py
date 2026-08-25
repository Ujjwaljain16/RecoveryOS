"""
PaymentProvider — the boundary between RecoveryOS's decisioning brain and
the outside world that actually moves money, TRD §1.3/§4.3.

Two implementations, one Protocol, one-line config swap
(settings.payment_provider_adapter — see get_provider_adapter()):

  - SimulatorAdapter: resolves outcomes from the SAME latent ground truth
    the Phase 1 simulator already computed for this payment
    (simulator_latent_state.true_recovery_prob_bps), stochastically. This
    is standing in for "the outside world responding" in demo mode — NOT
    the AI Diagnoser/propensity model reading latent state to make a
    decision (that boundary, inference_role/diagnoser_role having zero
    grant on this table, is unrelated and still fully enforced). A
    payment-gateway simulator is allowed to know how its own simulated
    world resolves; the decisioning models are not.
  - RazorpayTestAdapter: real HTTP calls to Razorpay's TEST-mode REST API.
    Without real test-mode credentials configured (this repo's default),
    calls genuinely fail with an auth error — consistent with this
    project's established pattern of letting real failures demonstrate
    resilience rather than faking success (see the AI Diagnoser's fallback
    path for the same principle).

Neither adapter is imported by services/policy_engine (which must stay
zero-I/O) — this lives entirely in the execution path
(workers/execution_worker.py), which is exactly where TRD §1.3 says
provider calls belong.
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection

from recoveryos.config import get_settings

logger = logging.getLogger(__name__)

Outcome = Literal["SUCCESS", "FAILED", "PENDING"]


@dataclass(frozen=True)
class ProviderResult:
    outcome: Outcome
    provider_ref: str | None
    recovered_amount_paise: int


class PaymentProvider(Protocol):
    """Every implementation must be safe to call more than once for the
    same payment_id without ill effect BEYOND what execute_with_idempotency
    already prevents — this protocol itself makes no idempotency promise;
    that guarantee lives entirely in services/execution_engine/idempotency.py,
    one layer up. A provider is just "make the attempt, report what
    happened."""

    def retry(
        self, conn: Connection, payment_id: str, amount_paise: int, attempt_number: int
    ) -> ProviderResult: ...


def resolve_simulated_outcome(true_recovery_prob_bps: int) -> bool:
    """
    THE single dice-roll that resolves a simulated ground-truth recovery
    probability into a real/counterfactual outcome. TRD §7's incremental-
    revenue comparison is only apples-to-apples if RecoveryOS's actual
    execution and the baseline strategy's counterfactual replay resolve
    outcomes through the IDENTICAL function — not two call sites that
    happen to run the same math today and silently drift the next time one
    of them gets refactored. SimulatorAdapter.retry() (this file) and
    services/pipeline/baseline.py's compute_and_persist_baseline_run() both
    call this exact function object; a test
    (tests/unit/test_resolve_simulated_outcome_shared.py) asserts that by
    monkeypatching it once and observing both call sites change together.
    """
    return random.uniform(0, 10_000) < true_recovery_prob_bps


def _recompute_attempt_aware_prob_bps(
    *,
    customer_patience_score: float,
    bank_latent_health: float,
    latent_customer_propensity: float,
    true_failure_type_value: str,
    attempt_number: int,
) -> int:
    """
    Re-derive true_recovery_prob_bps for the GIVEN attempt_number, using the
    exact same LatentRecoverabilityFunction the episode generator uses
    (simulator/outcomes/ground_truth.py) — not a re-hardcoded approximation
    of its decay curve.

    Why this is valid: simulator_latent_state rows reachable from the live
    pipeline are only ever created by the payments-mode PaymentGenerator
    (simulator/payments/generator.py:199), which always calls
    compute_latent_recovery(..., attempt_number=1, ...). At attempt_number=1
    the patience-decay factor is exp(-decay_rate * 0) == 1, so the STORED
    customer_patience_score IS customer.latent_patience_mean, undecayed —
    exactly the raw input compute_latent_recovery itself expects, letting
    it re-apply decay correctly for whatever attempt_number is passed here.
    If episode-mode's per-attempt latent rows (simulator/episodes/generator.py)
    are ever persisted to simulator_latent_state as well, this assumption
    would need re-deriving from the specific row's own recorded attempt.

    Only the recomputed PROBABILITY is used here — the function's own
    internal coin-flip (its returned `is_recoverable` bool) is discarded.
    The actual pass/fail decision still goes through
    resolve_simulated_outcome() exclusively (TRD §7's shared-resolver
    requirement is between SimulatorAdapter and baseline.py, not between
    this and the episode generator's own internal sampling).
    """
    from datetime import datetime, timezone

    from simulator.core.rng import SimRng
    from simulator.customers.generator import SimulatedCustomer
    from simulator.failures.codes import TrueFailureType
    from simulator.outcomes.ground_truth import LatentRecoverabilityFunction

    # Fresh RNG seed per call — this models a genuinely live, non-reproducible
    # retry attempt (not offline dataset generation), so there is no
    # determinism requirement to preserve here.
    rng = SimRng(uuid.uuid4().int & 0xFFFFFFFF)
    latent_function = LatentRecoverabilityFunction(rng)

    # Only latent_patience_mean and latent_propensity_bias are ever read by
    # compute_latent_recovery — every other field below is an unused
    # placeholder, never a real customer record (none is available at
    # execution time; only the derived latent snapshot is persisted).
    stub_customer = SimulatedCustomer(
        customer_id="",
        merchant_id="",
        is_returning=False,
        lifetime_value_paise=0,
        opted_out_at=None,
        created_at=datetime.now(timezone.utc),
        latent_patience_mean=customer_patience_score,
        latent_propensity_bias=latent_customer_propensity,
    )

    _is_recoverable, latent_record = latent_function.compute_latent_recovery(
        simulation_id="live-execution",
        latent_id=str(uuid.uuid4()),
        payment_id="live-execution",
        customer=stub_customer,
        true_failure_type=TrueFailureType(true_failure_type_value),
        latent_bank_health=bank_latent_health,
        attempt_number=attempt_number,
        timestamp=datetime.now(timezone.utc),
    )
    return latent_record.true_recovery_prob_bps


class SimulatorAdapter:
    """
    Demo-mode provider: resolves a retry's outcome from the SAME latent
    recoverability model the Phase 1 simulator uses
    (LatentRecoverabilityFunction.compute_latent_recovery), re-applying its
    attempt-based patience decay for whichever attempt_number this call
    represents — a live-executed attempt 3 gets genuinely decayed odds,
    not attempt 1's snapshot re-sampled unchanged (see
    _recompute_attempt_aware_prob_bps's docstring for why re-deriving this
    from the stored simulator_latent_state row is valid).

    If no latent state row exists for this payment (a genuinely live,
    non-synthetic payment has none), there is no ground truth to sample
    from — this is a real gap for a "demo mode against real payments"
    scenario, not silently faked: it returns PENDING rather than guessing.
    """

    def retry(
        self, conn: Connection, payment_id: str, amount_paise: int, attempt_number: int
    ) -> ProviderResult:
        row = conn.execute(
            text(
                "SELECT true_recovery_prob_bps, customer_patience_score, bank_latent_health, "
                "latent_customer_propensity, true_failure_type FROM simulator_latent_state "
                "WHERE payment_id = :pid ORDER BY created_at DESC LIMIT 1"
            ),
            {"pid": payment_id},
        ).mappings().first()

        if row is None:
            logger.warning(
                "[SimulatorAdapter] no simulator_latent_state for payment_id=%s — "
                "not a simulated payment, nothing to resolve against",
                payment_id,
            )
            return ProviderResult(outcome="PENDING", provider_ref=None, recovered_amount_paise=0)

        if attempt_number <= 1:
            # No decay to apply yet — use the stored value directly rather
            # than introducing a needless fresh-RNG recomputation for the
            # common case.
            true_recovery_prob_bps = row["true_recovery_prob_bps"]
        else:
            true_recovery_prob_bps = _recompute_attempt_aware_prob_bps(
                customer_patience_score=float(row["customer_patience_score"]),
                bank_latent_health=float(row["bank_latent_health"]),
                latent_customer_propensity=float(row["latent_customer_propensity"]),
                true_failure_type_value=row["true_failure_type"],
                attempt_number=attempt_number,
            )

        succeeded = resolve_simulated_outcome(true_recovery_prob_bps)
        return ProviderResult(
            outcome="SUCCESS" if succeeded else "FAILED",
            provider_ref=f"sim_{uuid.uuid4().hex[:16]}",
            recovered_amount_paise=amount_paise if succeeded else 0,
        )


_http_client: httpx.Client | None = None


def _get_shared_http_client() -> httpx.Client:
    """
    ONE pooled/keep-alive httpx.Client reused across every
    RazorpayTestAdapter instance and call, instead of a fresh TCP+TLS
    handshake per retry (a bare httpx.post(...) call opens and tears down
    a temporary client internally on every invocation). Process-lifetime
    singleton — get_provider_adapter() constructs a fresh RazorpayTestAdapter
    per call, but they all share this one client.
    """
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client()
    return _http_client


class RazorpayTestAdapter:
    """
    Real Razorpay TEST-mode integration. "Retrying" a failed payment on a
    real gateway means creating a fresh Order for the customer to complete
    (a gateway cannot force a specific declined card to succeed) — this
    calls Razorpay's real Orders API in test mode and reports whatever
    genuinely comes back.

    CORRECTED (previously wrong, caught on review — do not reintroduce):
    an earlier version of this adapter sent a fictional
    "X-Razorpay-Idempotency-Key" header. Verified against Razorpay's real
    docs (razorpay.com/docs/api/x/payout-idempotency, .../payments/route/
    direct-transfers-idempotent-request): Razorpay's dedicated idempotency
    HEADERS (X-Payout-Idempotency, X-Transfer-Idempotency, X-Refund-
    Idempotency) exist only for the Payouts/Transfers/Refunds APIs — there
    is no such header for Orders. The Orders API's real idempotency
    mechanism is the `receipt` field itself: creating a second order with a
    `receipt` value already used is rejected by Razorpay. `receipt` is set
    below to a value deterministic in payment_id for exactly this reason —
    that IS the real idempotency mechanism here, not a header.

    HONESTY NOTE: this has NOT been exercised against a live Razorpay
    sandbox in this session — no real RAZORPAY_KEY_ID/SECRET is configured
    (.env has none), and no network call to api.razorpay.com has actually
    been made or verified end-to-end. "Real HTTP calls" describes what the
    code does structurally, not that it has been proven against the real
    API. Treat this adapter as unverified integration code until someone
    with real test-mode credentials runs it once.

    OUTAGE FALLBACK (TRD §8 NFR table: "Provider Adapter degrades to
    Simulator on Razorpay test-API outage" — this is a named demo-day
    availability requirement, not a nice-to-have): both failure branches
    below (a raised httpx error, or a >=400 response) delegate to a real
    SimulatorAdapter instance instead of returning a bare, permanent
    PENDING. Logged distinctly (grep "RAZORPAY_OUTAGE_FALLBACK") so this
    is a visible, callable-out demo beat ("watch the system survive a real
    provider outage"), not a silently-swallowed failure.

    Reuses ONE httpx.Client (module-level, connection-pooled/keep-alive)
    across every call instead of a fresh TCP+TLS handshake per retry —
    this method is called from inside a held Postgres advisory lock
    (services/execution_engine/idempotency.py) with BATCH_SIZE=1
    (workers/execution_worker.py), so avoidable per-call latency here
    directly bounds worker throughput.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._key_id = settings.razorpay_key_id
        self._key_secret = settings.razorpay_key_secret
        self._base_url = settings.razorpay_base_url
        self._timeout = settings.razorpay_timeout_seconds
        self._client = _get_shared_http_client()
        self._fallback = SimulatorAdapter()

    def retry(
        self, conn: Connection, payment_id: str, amount_paise: int, attempt_number: int
    ) -> ProviderResult:
        try:
            response = self._client.post(
                f"{self._base_url}/orders",
                auth=(self._key_id, self._key_secret),
                json={
                    "amount": amount_paise,
                    "currency": "INR",
                    # The real idempotency mechanism (see class docstring) —
                    # deterministic per (payment_id, attempt_number), NOT a
                    # header. MUST include attempt_number: a legitimate
                    # attempt 2 is a distinct idempotency_key one layer up
                    # (workers/execution_worker.py's
                    # recovery:{payment_id}:{action_type}:{attempt_number})
                    # and must not collide with attempt 1's receipt, or
                    # Razorpay itself would reject the real retry as a
                    # duplicate of the first attempt.
                    "receipt": f"recovery_{payment_id}_{attempt_number}",
                    "notes": {"recovery_of_payment_id": payment_id, "attempt_number": str(attempt_number)},
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("[RazorpayTestAdapter] request failed: %s: %s", type(exc).__name__, exc)
            return self._fallback_to_simulator(conn, payment_id, amount_paise, attempt_number, reason=str(exc))

        if response.status_code >= 400:
            logger.warning(
                "[RazorpayTestAdapter] order creation failed: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            return self._fallback_to_simulator(
                conn, payment_id, amount_paise, attempt_number, reason=f"status={response.status_code}"
            )

        body = response.json()
        # A created order is not itself a completed payment — the customer
        # still has to pay it. This adapter reports the order as PENDING
        # (a real recovery attempt now exists); resolving to SUCCESS/FAILED
        # is a webhook/polling concern outside this call's scope.
        return ProviderResult(
            outcome="PENDING",
            provider_ref=body.get("id"),
            recovered_amount_paise=0,
        )

    def _fallback_to_simulator(
        self, conn: Connection, payment_id: str, amount_paise: int, attempt_number: int, *, reason: str
    ) -> ProviderResult:
        logger.warning(
            "RAZORPAY_OUTAGE_FALLBACK payment_id=%s attempt_number=%s reason=%s -- "
            "degrading to SimulatorAdapter per TRD §8's availability guarantee",
            payment_id,
            attempt_number,
            reason,
        )
        return self._fallback.retry(conn, payment_id, amount_paise, attempt_number)


_ADAPTERS = {
    "simulator": SimulatorAdapter,
    "razorpay_test": RazorpayTestAdapter,
}


def get_provider_adapter() -> PaymentProvider:
    """
    The ONE line that swaps providers: settings.payment_provider_adapter.
    No other code path branches on env/is_demo for this — see
    test_provider_adapter_swap_is_config_only.
    """
    settings = get_settings()
    adapter_cls = _ADAPTERS.get(settings.payment_provider_adapter)
    if adapter_cls is None:
        raise ValueError(
            f"Unknown payment_provider_adapter={settings.payment_provider_adapter!r}, "
            f"expected one of {list(_ADAPTERS)}"
        )
    return adapter_cls()
