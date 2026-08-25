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

    def retry(self, conn: Connection, payment_id: str, amount_paise: int) -> ProviderResult: ...


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


class SimulatorAdapter:
    """
    Demo-mode provider: resolves a retry's outcome from
    simulator_latent_state.true_recovery_prob_bps for this exact payment —
    the same latent recoverability the Phase 1 simulator already computed
    at payment-generation time — sampled stochastically, exactly like the
    episode generator itself resolves a retry attempt
    (simulator/episodes/generator.py: `rng.uniform("latent") < retry_prob`).

    If no latent state row exists for this payment (a genuinely live,
    non-synthetic payment has none), there is no ground truth to sample
    from — this is a real gap for a "demo mode against real payments"
    scenario, not silently faked: it returns PENDING rather than guessing.
    """

    def retry(self, conn: Connection, payment_id: str, amount_paise: int) -> ProviderResult:
        row = conn.execute(
            text(
                "SELECT true_recovery_prob_bps FROM simulator_latent_state "
                "WHERE payment_id = :pid ORDER BY created_at DESC LIMIT 1"
            ),
            {"pid": payment_id},
        ).first()

        if row is None:
            logger.warning(
                "[SimulatorAdapter] no simulator_latent_state for payment_id=%s — "
                "not a simulated payment, nothing to resolve against",
                payment_id,
            )
            return ProviderResult(outcome="PENDING", provider_ref=None, recovered_amount_paise=0)

        true_recovery_prob_bps = row[0]
        succeeded = resolve_simulated_outcome(true_recovery_prob_bps)
        return ProviderResult(
            outcome="SUCCESS" if succeeded else "FAILED",
            provider_ref=f"sim_{uuid.uuid4().hex[:16]}",
            recovered_amount_paise=amount_paise if succeeded else 0,
        )


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
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._key_id = settings.razorpay_key_id
        self._key_secret = settings.razorpay_key_secret
        self._base_url = settings.razorpay_base_url

    def retry(self, conn: Connection, payment_id: str, amount_paise: int) -> ProviderResult:
        try:
            response = httpx.post(
                f"{self._base_url}/orders",
                auth=(self._key_id, self._key_secret),
                json={
                    "amount": amount_paise,
                    "currency": "INR",
                    # The real idempotency mechanism (see class docstring) —
                    # deterministic per payment_id, NOT a header.
                    "receipt": f"recovery_{payment_id}",
                    "notes": {"recovery_of_payment_id": payment_id},
                },
                timeout=10,
            )
        except httpx.HTTPError as exc:
            logger.warning("[RazorpayTestAdapter] request failed: %s: %s", type(exc).__name__, exc)
            return ProviderResult(outcome="PENDING", provider_ref=None, recovered_amount_paise=0)

        if response.status_code >= 400:
            logger.warning(
                "[RazorpayTestAdapter] order creation failed: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            return ProviderResult(outcome="PENDING", provider_ref=None, recovered_amount_paise=0)

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
