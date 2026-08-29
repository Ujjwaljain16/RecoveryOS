"""
Domain Audit finding #3 -- the real execution boundary, proven per
action_type (real Postgres, spy provider/notification/handoff services so
we can assert exactly which one was called, not just that SOMETHING
happened):

    RETRY_NOW / ALT_ROUTE -> PaymentProvider.retry()   (real money)
    REMINDER              -> NotificationService        (never money)
    ESCALATE              -> HumanHandoffService         (never money)

Before this fix, every action_type reached the exact same
PaymentProvider.retry() call -- these tests would have failed against the
old code (REMINDER/ESCALATE would have shown provider.retry() called).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text

from services.execution_engine.human_handoff import HandoffResult
from services.execution_engine.notification import NotificationResult
from tests.integration.test_execution_worker import _seed_decision_fk_chain, _seed_failed_payment
from workers.execution_worker import process_job


class SpyProvider:
    def __init__(self):
        self.calls: list[tuple] = []

    def retry(self, conn, payment_id, amount_paise, attempt_number):
        from integrations.razorpay.adapter import ProviderResult

        self.calls.append((payment_id, amount_paise, attempt_number))
        return ProviderResult(
            outcome="SUCCESS",
            provider_ref=f"order_{uuid.uuid4().hex[:8]}",
            recovered_amount_paise=amount_paise,
        )


class SpyNotificationService:
    def __init__(self):
        self.calls: list[tuple] = []

    def send_reminder(self, conn, payment_id, amount_paise, attempt_number):
        self.calls.append((payment_id, amount_paise, attempt_number))
        return NotificationResult(
            outcome="SUCCESS",
            provider_ref=f"notif_{uuid.uuid4().hex[:8]}",
            recovered_amount_paise=0,
        )


class SpyHumanHandoffService:
    def __init__(self):
        self.calls: list[tuple] = []

    def create_escalation(self, conn, payment_id, amount_paise, attempt_number):
        self.calls.append((payment_id, amount_paise, attempt_number))
        return HandoffResult(
            outcome="SUCCESS", provider_ref=f"esc_{uuid.uuid4().hex[:8]}", recovered_amount_paise=0
        )


def _make_job(
    payment_id: str, decision_id: str, action_type: str, amount_paise: int = 100_000
) -> dict:
    return {
        "payment_id": payment_id,
        "idempotency_key": f"recovery:{payment_id}:{action_type}:1",
        "action_type": action_type,
        "attempt_number": 1,
        "decision_id": decision_id,
        "amount_paise": amount_paise,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("action_type", ["RETRY_NOW", "ALT_ROUTE"])
async def test_money_moving_actions_call_the_payment_provider_only(migrated_db, action_type):
    payment_id = await _seed_failed_payment(migrated_db)
    decision_id = await _seed_decision_fk_chain(migrated_db, payment_id, 100_000)
    job = _make_job(payment_id, decision_id, action_type)

    engine = create_engine(migrated_db, pool_pre_ping=True)
    provider = SpyProvider()
    notification_service = SpyNotificationService()
    human_handoff_service = SpyHumanHandoffService()

    with engine.connect() as conn:
        process_job(
            conn,
            job,
            provider=provider,
            notification_service=notification_service,
            human_handoff_service=human_handoff_service,
        )

    assert len(provider.calls) == 1, f"{action_type} must call PaymentProvider.retry() exactly once"
    assert provider.calls[0] == (payment_id, 100_000, 1)
    assert notification_service.calls == [], f"{action_type} must never call NotificationService"
    assert human_handoff_service.calls == [], f"{action_type} must never call HumanHandoffService"
    engine.dispose()


@pytest.mark.asyncio
async def test_reminder_calls_notification_service_never_the_payment_provider(migrated_db):
    payment_id = await _seed_failed_payment(migrated_db)
    decision_id = await _seed_decision_fk_chain(migrated_db, payment_id, 100_000)
    job = _make_job(payment_id, decision_id, "REMINDER")

    engine = create_engine(migrated_db, pool_pre_ping=True)
    provider = SpyProvider()
    notification_service = SpyNotificationService()
    human_handoff_service = SpyHumanHandoffService()

    with engine.connect() as conn:
        process_job(
            conn,
            job,
            provider=provider,
            notification_service=notification_service,
            human_handoff_service=human_handoff_service,
        )

    assert (
        provider.calls == []
    ), "REMINDER must NEVER call PaymentProvider.retry() -- this is the exact bug the audit found"
    assert len(notification_service.calls) == 1
    assert notification_service.calls[0] == (payment_id, 100_000, 1)
    assert human_handoff_service.calls == []

    with engine.connect() as conn:
        recovered = (
            conn.execute(
                text(
                    "SELECT recovered_amount_paise, outcome FROM recoveries WHERE payment_id = :pid"
                ),
                {"pid": payment_id},
            )
            .mappings()
            .first()
        )
    engine.dispose()

    assert (
        recovered["recovered_amount_paise"] == 0
    ), "a notification must never itself recover money"


@pytest.mark.asyncio
async def test_escalate_calls_human_handoff_service_never_the_payment_provider(migrated_db):
    payment_id = await _seed_failed_payment(migrated_db)
    decision_id = await _seed_decision_fk_chain(migrated_db, payment_id, 100_000)
    job = _make_job(payment_id, decision_id, "ESCALATE")

    engine = create_engine(migrated_db, pool_pre_ping=True)
    provider = SpyProvider()
    notification_service = SpyNotificationService()
    human_handoff_service = SpyHumanHandoffService()

    with engine.connect() as conn:
        process_job(
            conn,
            job,
            provider=provider,
            notification_service=notification_service,
            human_handoff_service=human_handoff_service,
        )

    assert provider.calls == [], "ESCALATE must NEVER call PaymentProvider.retry()"
    assert notification_service.calls == []
    assert len(human_handoff_service.calls) == 1
    assert human_handoff_service.calls[0] == (payment_id, 100_000, 1)

    with engine.connect() as conn:
        recovered = conn.execute(
            text("SELECT recovered_amount_paise FROM recoveries WHERE payment_id = :pid"),
            {"pid": payment_id},
        ).scalar_one()
    engine.dispose()

    assert recovered == 0, "an escalation must never itself recover money"


@pytest.mark.asyncio
async def test_demo_notification_adapter_records_a_real_simulated_events_row(migrated_db):
    """The REAL adapter (not a spy) -- proves the events row itself is
    correctly shaped, independent of the process_job routing tested above."""
    from services.execution_engine.notification import DemoNotificationAdapter

    payment_id = await _seed_failed_payment(migrated_db)
    engine = create_engine(migrated_db, pool_pre_ping=True)
    with engine.connect() as conn:
        DemoNotificationAdapter().send_reminder(conn, payment_id, 100_000, 1)
        conn.commit()

    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT payload FROM events WHERE payment_id = :pid AND event_type = 'NOTIFICATION_SENT'"
                ),
                {"pid": payment_id},
            )
            .mappings()
            .first()
        )
    engine.dispose()

    assert row is not None
    payload = row["payload"]
    assert payload["notification_type"] == "PAYMENT_RETRY_REMINDER"
    assert (
        payload["status"] == "simulated"
    ), "must never claim 'sent' -- this is a safe observable stub, not a real integration"


@pytest.mark.asyncio
async def test_demo_human_handoff_adapter_records_a_real_recorded_events_row(migrated_db):
    from services.execution_engine.human_handoff import DemoHumanHandoffAdapter

    payment_id = await _seed_failed_payment(migrated_db)
    engine = create_engine(migrated_db, pool_pre_ping=True)
    with engine.connect() as conn:
        DemoHumanHandoffAdapter().create_escalation(conn, payment_id, 100_000, 1)
        conn.commit()

    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT payload FROM events WHERE payment_id = :pid AND event_type = 'ESCALATION_CREATED'"
                ),
                {"pid": payment_id},
            )
            .mappings()
            .first()
        )
    engine.dispose()

    assert row is not None
    assert row["payload"]["status"] == "recorded"


@pytest.mark.asyncio
async def test_reminder_never_reaches_the_real_payment_provider_even_without_a_spy(migrated_db):
    """Negative control against the DEFAULT (no injected provider) path --
    proves the real DemoNotificationAdapter is actually wired in
    production code, not just reachable when a test happens to inject a
    spy. Uses no provider at all (None) -- if REMINDER ever fell through
    to the real get_provider_adapter() path, this would either explode
    (no configured provider) or silently create a real order, either of
    which this test would catch."""
    payment_id = await _seed_failed_payment(migrated_db)
    decision_id = await _seed_decision_fk_chain(migrated_db, payment_id, 100_000)
    job = _make_job(payment_id, decision_id, "REMINDER")

    engine = create_engine(migrated_db, pool_pre_ping=True)
    with engine.connect() as conn:
        result = process_job(
            conn, job
        )  # no provider/notification_service/human_handoff_service injected

    assert result["recovered_amount_paise"] == 0

    with engine.connect() as conn:
        recovery_row = conn.execute(
            text("SELECT provider_ref FROM recoveries WHERE payment_id = :pid"), {"pid": payment_id}
        ).scalar_one()
    engine.dispose()

    assert recovery_row is not None and not recovery_row.startswith("order_"), (
        "REMINDER's provider_ref must come from the real DemoNotificationAdapter "
        "(a notif_/event-id-shaped ref), never a real Razorpay/Simulator order id"
    )
