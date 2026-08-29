"""
Production Architecture Domain Audit, Finding #3 -- all three background
processes were strictly single-replica in docker-compose.yml (fixed
container_name + published host ports both block `docker compose up
--scale`), with no proof the underlying Redis consumer-group mechanics
(already used for crash-recovery via XAUTOCLAIM) would actually
distribute work across multiple replicas rather than duplicate it.

Fixed by removing the container_name/host-port blockers from
pipeline_orchestrator, execution_worker, and retry_scheduler (docker-
compose.yml) -- `docker compose up --scale pipeline_orchestrator=3` now
works. This test proves the mechanism itself directly and deterministically:
two "replicas" (real, distinct XREADGROUP consumer names, sharing the
SAME real consumer group RecoveryOS's own pipeline_orchestrator uses)
reading from the same stream must receive DISJOINT message sets that
together cover every message -- not each replica processing everything
(duplication) and not any message going unclaimed.

Explicitly scoped per the approved plan: this proves REDIS-LEVEL work
distribution exists and works. It does NOT measure or change LLM-hot-path
throughput -- that stays parked pending a real "1 replica -> X events/sec,
2 replicas -> Y events/sec" measurement, not a code change made without data.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest

from services.pipeline.consumer import GROUP_NAME, STREAM_NAME


async def _ensure_group(redis_client, stream: str, group: str) -> None:
    with contextlib.suppress(Exception):
        await redis_client.xgroup_create(stream, group, id="0", mkstream=True)


@pytest.mark.asyncio
async def test_two_consumer_replicas_in_the_same_group_split_work_without_duplication(redis_client):
    """
    The real, exact mechanism a second pipeline_orchestrator container
    would use: same STREAM_NAME/GROUP_NAME this system's real consumer
    already reads from, two distinct consumer names (exactly what
    hostname+pid-derived CONSUMER_NAME produces per replica), real
    XREADGROUP calls -- not a simulation of Redis's guarantee, the actual
    guarantee itself, exercised against this system's real stream/group
    identity.
    """
    stream = f"{STREAM_NAME}:replica-test:{uuid.uuid4().hex[:8]}"
    group = GROUP_NAME
    await _ensure_group(redis_client, stream, group)

    message_ids = []
    for i in range(20):
        msg_id = await redis_client.xadd(stream, {"payment_id": str(uuid.uuid4()), "seq": str(i)})
        message_ids.append(msg_id)

    # Two "replicas" -- real distinct consumer names in the SAME group,
    # each doing exactly what run_consumer's own xreadgroup call does,
    # ALTERNATING small reads (count=1) -- this is what genuinely happens
    # when two real replica processes are both concurrently polling the
    # same group: whichever XREADGROUP call reaches Redis first claims
    # the next available message, not a fixed split decided in advance.
    # A single large-count burst read by one caller would just drain
    # everything available regardless of how many "replicas" exist --
    # that's not duplication, it's the OTHER caller finding nothing left,
    # which is what interleaving the reads avoids conflating.
    ids_a: set[str] = set()
    ids_b: set[str] = set()
    for i in range(len(message_ids)):
        consumer_name, target = (
            ("pipeline_orchestrator-replica-a", ids_a)
            if i % 2 == 0
            else ("pipeline_orchestrator-replica-b", ids_b)
        )
        result = await redis_client.xreadgroup(
            groupname=group, consumername=consumer_name, streams={stream: ">"}, count=1
        )
        for _s, msgs in result:
            for msg_id, _raw in msgs:
                target.add(msg_id)

    assert ids_a, "replica A must have received at least some messages"
    assert ids_b, "replica B must have received at least some messages"
    assert ids_a.isdisjoint(ids_b), (
        f"the SAME message must never be delivered to two different consumers in the same "
        f"group simultaneously -- overlap: {ids_a & ids_b}"
    )
    assert ids_a | ids_b == set(
        message_ids
    ), "every message must be claimed by exactly one replica -- none lost, none duplicated"

    # Cleanup: ack everything so this test's messages don't linger.
    for msg_id in message_ids:
        await redis_client.xack(stream, group, msg_id)


@pytest.mark.asyncio
async def test_a_crashed_replicas_unacked_message_is_reclaimable_by_a_surviving_replica(
    redis_client,
):
    """
    The other half of the replica story: if one replica dies mid-message
    (never XACKs), a SURVIVING replica must be able to reclaim it via
    XAUTOCLAIM -- the exact mechanism services/pipeline/consumer.py's own
    _reclaim_pending already implements and this system already relies on
    for single-replica crash recovery. This proves it also correctly
    hands the reclaimed message to a DIFFERENT consumer name (a second
    replica), not just the original one restarting.
    """
    stream = f"{STREAM_NAME}:reclaim-test:{uuid.uuid4().hex[:8]}"
    group = GROUP_NAME
    await _ensure_group(redis_client, stream, group)

    msg_id = await redis_client.xadd(stream, {"payment_id": str(uuid.uuid4())})

    # Replica A reads it but "crashes" -- never XACKs.
    await redis_client.xreadgroup(
        groupname=group,
        consumername="pipeline_orchestrator-crashed-replica",
        streams={stream: ">"},
        count=1,
    )

    # Replica B (a different, surviving replica) reclaims it via
    # XAUTOCLAIM with min_idle_time=0 -- immediate reclaim for this test,
    # matching _reclaim_pending's own real startup behavior.
    next_id, reclaimed, _deleted = await redis_client.xautoclaim(
        stream, group, "pipeline_orchestrator-surviving-replica", min_idle_time=0, start_id="0-0"
    )

    reclaimed_ids = {m_id for m_id, _raw in reclaimed}
    assert (
        msg_id in reclaimed_ids
    ), "a surviving replica must be able to reclaim a crashed replica's message"

    await redis_client.xack(stream, group, msg_id)
