"""
Task 6 originally proved risk/audit/experiments/incidents honestly 501'd
rather than faking success. Phase 9 (dashboard) wired all of them for
real — this file now proves the OPPOSITE: each of these routes returns
real, DB-sourced data (zeroed for a merchant with no data yet, never a
fabricated non-zero placeholder), auth still runs before any route logic,
and a nonexistent resource still 404s rather than silently succeeding.
"""

from __future__ import annotations

import uuid

import pytest

from apps.api.dependencies.auth import generate_api_key
from tests.integration.conftest import seed_merchant_with_api_key

# Routes that now have real implementations (Phase 9) — used to 501 as a
# deliberate "honest gate," per Task 6.
WIRED_ROUTES = [
    ("GET", "/v1/risk/summary"),
    ("GET", "/v1/audit/{payment_id}"),
    ("GET", "/v1/incidents/active"),
]


async def _seeded_merchant(migrated_db: str) -> tuple[str, str]:
    merchant_id = str(uuid.uuid4())
    raw_key = generate_api_key()
    await seed_merchant_with_api_key(migrated_db, merchant_id, "stub-route-test-merchant", raw_key)
    return merchant_id, raw_key


@pytest.mark.asyncio
async def test_wired_routes_return_real_zeroed_data_for_a_fresh_merchant(async_client, migrated_db):
    """
    A brand-new merchant with zero payments must see real, honestly-zero
    aggregates (200 OK) — not a 501 (that behavior is retired) and not a
    fabricated non-zero number.
    """
    _merchant_id, api_key = await _seeded_merchant(migrated_db)
    headers = {"X-API-Key": api_key}

    risk_resp = await async_client.get("/v1/risk/summary", headers=headers)
    assert risk_resp.status_code == 200
    risk_body = risk_resp.json()
    assert risk_body["revenue_at_risk_paise"] == 0
    assert risk_body["recovered_paise"] == 0
    assert risk_body["incremental_recovery_paise"] == 0
    assert risk_body["recovery_rate_bps"] == 0
    assert risk_body["recovery_queue"] == []

    # /v1/incidents/active is intentionally platform-wide, not merchant-
    # scoped (anomaly_windows.scope_type='bank' has no merchant column —
    # same reasoning as risk.py's bank health grid), so it can legitimately
    # be non-empty here if some OTHER test in the same full-suite run
    # already triggered a fresh high-severity window. Only the SHAPE is
    # real to assert for a brand-new merchant, not global emptiness.
    incidents_resp = await async_client.get("/v1/incidents/active", headers=headers)
    assert incidents_resp.status_code == 200
    assert "incidents" in incidents_resp.json()

    audit_resp = await async_client.get(f"/v1/audit/{uuid.uuid4()}", headers=headers)
    assert audit_resp.status_code == 404, "a nonexistent payment must still 404, not fake-succeed"


@pytest.mark.asyncio
async def test_experiments_live_is_real_zeroed_and_unknown_run_id_404s(async_client, migrated_db):
    _merchant_id, api_key = await _seeded_merchant(migrated_db)
    headers = {"X-API-Key": api_key}

    live_resp = await async_client.get("/v1/experiments/live", headers=headers)
    assert live_resp.status_code == 200
    live_body = live_resp.json()
    assert live_body["dataset_size"] == 0
    assert live_body["incremental_recovery_paise"] == 0

    unknown_resp = await async_client.get(f"/v1/experiments/{uuid.uuid4()}", headers=headers)
    assert unknown_resp.status_code == 404


@pytest.mark.asyncio
async def test_experiments_phase8_baseline_serves_the_real_multi_seed_artifact(
    async_client, migrated_db
):
    """
    tests/evaluation/artifacts/multi_seed_compliance_aware_aggregate.json's
    5-seed compliance-aware study -- the SAME artifact README.md's own
    headline number is sourced from -- must be served verbatim off disk,
    not recomputed or guessed. Reads the artifact directly here (rather
    than hardcoding a magic number) so this test can never itself drift
    from whatever the artifact currently says, the same "read verbatim"
    discipline the endpoint itself is held to.
    """
    import json
    from pathlib import Path

    artifact_path = (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "evaluation"
        / "artifacts"
        / "multi_seed_compliance_aware_aggregate.json"
    )
    with artifact_path.open() as f:
        artifact = json.load(f)
    seed_1_expected = next(s for s in artifact["per_seed_results"] if s["seed"] == 1)
    expected_incremental = seed_1_expected["incremental_recoveryos_vs_compliance_aware_fair_paise"]

    _merchant_id, api_key = await _seeded_merchant(migrated_db)
    resp = await async_client.get("/v1/experiments/phase8-baseline", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["seeds"]) == 5
    seed_1 = next(s for s in body["seeds"] if s["seed"] == 1)
    assert seed_1["incremental_recovery_paise"] == expected_incremental
    # Mean incremental recovery must match the artifact's own precomputed
    # aggregate exactly (this is what README.md's headline number is), not
    # a placeholder zero or an independently-recomputed value that could
    # silently disagree with it.
    assert body["incremental_recovery_paise_mean"] == round(
        artifact["aggregate"]["mean_incremental_recovery_paise"]
    )
    assert body["incremental_recovery_paise_mean"] > 0


@pytest.mark.asyncio
async def test_experiments_phase8_baseline_unnecessary_intervention_rate_field_survives_artifact_rename(
    async_client, migrated_db
):
    """
    Regression coverage for a class of bug this endpoint has hit twice now
    (the artifact backing it changed shape/fields under it): this pins the
    OUTPUT contract narrowly, not the internal computation.
    apps/dashboard/app/experiments/page.tsx's field name
    (unnecessary_intervention_rate_bps) must stay present and a sane
    basis-points value regardless of which real fields
    _phase8_baseline_experiment() currently derives it from -- see that
    function's own docstring for what it computes today (a proxy: the
    fraction of RecoveryOS's own interventions that did not end in a
    recovery) and why it's no longer the same metric the original Phase 8
    artifact exposed under this name.
    """
    _merchant_id, api_key = await _seeded_merchant(migrated_db)
    resp = await async_client.get("/v1/experiments/phase8-baseline", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    body = resp.json()
    rate_bps = body["recoveryos"]["unnecessary_intervention_rate_bps"]
    assert isinstance(rate_bps, int)
    assert 0 <= rate_bps <= 10_000, f"expected a basis-points rate in [0, 10000], got {rate_bps}"


@pytest.mark.asyncio
async def test_wired_routes_401_before_ever_reaching_route_logic(async_client):
    """
    Auth still runs first: an unauthenticated caller gets 401, never a 200
    (or a 404) that would imply the route ran without ever checking who's
    asking.
    """
    for method, path_template in WIRED_ROUTES:
        path = path_template.format(payment_id=str(uuid.uuid4()))
        resp = await async_client.request(method, path)
        assert resp.status_code == 401, f"{method} {path} without a key must 401"

    resp = await async_client.get(f"/v1/experiments/{uuid.uuid4()}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_payments_detail_is_real_not_gated(async_client, migrated_db):
    """
    payments/{id}/detail has been real since Task 6 — confirms it still
    does NOT 501 (a nonexistent payment_id correctly 404s instead).
    """
    _merchant_id, api_key = await _seeded_merchant(migrated_db)
    resp = await async_client.get(
        f"/v1/payments/{uuid.uuid4()}/detail", headers={"X-API-Key": api_key}
    )
    assert resp.status_code == 404, (
        f"A nonexistent payment should 404 (real lookup, real absence), not "
        f"501 — got {resp.status_code}: {resp.text}"
    )
