from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import runtime_task_worker as worker


PROMPT_LANES = (
    "web",
    "direct-business_task",
    "direct-trigger",
    "direct-delegation",
    "session-events",
    "channel-delivery",
    "budget-transition",
    "runtime-notification",
    "turn-replacement",
    "input-dispatch",
    "terminal-input",
    "control-input",
)


@pytest.mark.parametrize("lane", PROMPT_LANES)
async def test_empty_tick_skips_consumer_then_handles_unwoken_work_within_its_period(monkeypatch, lane):
    # Poll lanes never owned the claim wake Event. Round 2 restores that
    # boundary; immediate wake assertions belong to the unchanged claim loop.
    # Keep every polling lane's latency assertion, separately from PG shapes.
    poll = 0.04
    idle, handled = asyncio.Event(), asyncio.Event()
    available, calls = False, []

    async def due(**_kwargs):
        if not available:
            idle.set()
        return {lane: available}

    async def drain():
        assert available
        calls.append(lane)
        handled.set()

    monkeypatch.setattr(worker, "_STATE", {})
    monkeypatch.setattr(worker, "_has_due_runtime_work", due)
    monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda _: {lane: (drain, poll)})
    running = asyncio.create_task(worker.start_terminal_boundary_worker_loop(worker_id="a2q"))
    try:
        await asyncio.wait_for(idle.wait(), 1)
        assert calls == []
        available = True
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(handled.wait(), poll + 0.03)
        assert asyncio.get_running_loop().time() - started < poll + 0.03
        assert calls == [lane]
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_existing_redis_publish_wakes_claim_without_waiting_for_real_poll(monkeypatch):
    from app.core.events import get_redis

    first, handled = asyncio.Event(), asyncio.Event()
    calls = []

    async def claim(**_kwargs):
        calls.append(1)
        (first if len(calls) == 1 else handled).set()
        return []

    async def coordinator(**_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "_STATE", {})
    monkeypatch.setattr(worker, "_LOCAL_WAKEUP_EVENT", None)
    monkeypatch.setattr(worker, "claim_and_dispatch_once", claim)
    monkeypatch.setattr(worker, "start_terminal_boundary_worker_loop", coordinator)
    running = asyncio.create_task(worker.start_runtime_task_worker_loop())
    redis = await get_redis()
    channel = worker._settings().RUNTIME_TASK_WAKEUP_CHANNEL
    try:
        await asyncio.wait_for(first.wait(), 1)
        async with asyncio.timeout(1):
            while not any(count for _name, count in await redis.pubsub_numsub(channel)):
                await asyncio.sleep(0.005)
        await redis.publish(channel, '{"reason":"a2q-local-test"}')
        await asyncio.wait_for(handled.wait(), 0.2)
        assert len(calls) == 2
        assert worker._STATE["wakeup_received"] == 1
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


def test_poll_only_lanes_keep_their_periods(monkeypatch):
    monkeypatch.setattr(worker, "_settings", lambda: SimpleNamespace(RUNTIME_TASK_CLAIM_POLL_SECONDS=1))
    table = worker._terminal_boundary_lanes("a2q")
    assert {lane: table[lane][1] for lane in PROMPT_LANES} == dict.fromkeys(PROMPT_LANES, 1)
    assert {lane: period for lane, (_drain, period) in table.items() if lane not in PROMPT_LANES} == dict.fromkeys(
        (
            "stale-admission",
            "model-round",
            "terminal-outcome",
            "permission-expiry",
            "hr-draft",
            "business-task",
            "team-fanout",
        ),
        20,
    )
