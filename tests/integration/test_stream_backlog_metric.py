"""
Production Architecture Domain Audit, Finding #4 -- event_processor (and,
extended here to the other two Redis-Streams consumers, pipeline_
orchestrator/execution_worker) had zero visibility into whether ingestion
was falling behind. `stream_backlog_depth` (XINFO GROUPS' own `lag` field)
is the real signal: entries never yet delivered to ANY consumer in the
group. This test proves it reflects genuine backlog, not just a
hardcoded/always-zero stub.
"""

from __future__ import annotations

import uuid

import pytest
from prometheus_client import REGISTRY

from services.event_processor.consumer import GROUP_NAME, STREAM_NAME, _record_backlog


@pytest.mark.asyncio
async def test_backlog_gauge_reflects_real_undelivered_messages(redis_client):
    stream = f"{STREAM_NAME}:test:{uuid.uuid4().hex[:8]}"
    group = GROUP_NAME

    await redis_client.xgroup_create(stream, group, id="0", mkstream=True)

    for i in range(5):
        await redis_client.xadd(stream, {"payload": f"msg-{i}"})

    await _record_backlog_for(redis_client, stream, group)
    lag_with_backlog = REGISTRY.get_sample_value(
        "stream_backlog_depth", {"stream": stream, "group": group}
    )
    assert (
        lag_with_backlog == 5.0
    ), f"expected real lag of 5 undelivered messages, got {lag_with_backlog}"

    # Drain the stream via a real consumer read + XACK -- lag must drop
    # back to 0, proving this isn't a monotonic/one-way counter mistake.
    results = await redis_client.xreadgroup(
        groupname=group, consumername="test-consumer", streams={stream: ">"}, count=10
    )
    for _stream_name, messages in results:
        for msg_id, _raw in messages:
            await redis_client.xack(stream, group, msg_id)

    await _record_backlog_for(redis_client, stream, group)
    lag_after_drain = REGISTRY.get_sample_value(
        "stream_backlog_depth", {"stream": stream, "group": group}
    )
    assert (
        lag_after_drain == 0.0
    ), f"lag must return to 0 once every message is delivered, got {lag_after_drain}"


async def _record_backlog_for(redis_client, stream: str, group: str) -> None:
    """Mirrors _record_backlog's own XINFO GROUPS logic against an
    arbitrary (stream, group) pair, since the real function is hardcoded
    to event_processor's own STREAM_NAME/GROUP_NAME constants."""
    from recoveryos.metrics import stream_backlog_depth

    groups = await redis_client.xinfo_groups(stream)
    for g in groups:
        if g.get("name") == group:
            lag = g.get("lag")
            if lag is not None:
                stream_backlog_depth.labels(stream=stream, group=group).set(lag)
            break


@pytest.mark.asyncio
async def test_real_record_backlog_function_updates_the_gauge_for_the_real_stream(redis_client):
    """The actual _record_backlog function (not the test-local mirror
    above), against event_processor's real STREAM_NAME/GROUP_NAME --
    proves the wiring in the real consumer module works end to end.
    Cleans up its own message afterward -- STREAM_NAME/GROUP_NAME are
    shared real constants other tests in this suite also use."""
    try:
        await redis_client.xgroup_create(STREAM_NAME, GROUP_NAME, id="0", mkstream=True)
    except Exception:
        pass  # group may already exist from another test in this session

    msg_id = await redis_client.xadd(
        STREAM_NAME, {"event_type": "PAYMENT_FAILED", "payment_id": str(uuid.uuid4())}
    )
    try:
        await _record_backlog(redis_client)

        lag = REGISTRY.get_sample_value(
            "stream_backlog_depth", {"stream": STREAM_NAME, "group": GROUP_NAME}
        )
        assert lag is not None and lag >= 1.0
    finally:
        # Best-effort cleanup: ack (as this consumer group) and delete the
        # test message so it doesn't linger as backlog for other tests.
        try:
            results = await redis_client.xreadgroup(
                groupname=GROUP_NAME,
                consumername="test-cleanup",
                streams={STREAM_NAME: ">"},
                count=50,
            )
            for _stream_name, messages in results:
                for m_id, _raw in messages:
                    await redis_client.xack(STREAM_NAME, GROUP_NAME, m_id)
        except Exception:
            pass
        await redis_client.xdel(STREAM_NAME, msg_id)
