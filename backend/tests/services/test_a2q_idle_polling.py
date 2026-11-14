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


@pytest.mark.parametrize("lane", (*PROMPT_LANES, "claim"))
@pytest.mark.parametrize("wake", (False, True), ids=("poll-backstop", "existing-wakeup"))
async def test_empty_tick_skips_consumers_then_handles_due_work_within_existing_latency(monkeypatch, lane, wake):
    # Scheduler-only test. PostgreSQL tests below separately prove that the
    # actual combined read detects each candidate shape under enforced RLS.
    poll = 0.3 if wake else 0.04
    idle, handled = asyncio.Event(), asyncio.Event()
    event = asyncio.Event()
    available = False
    reads, calls = [], []

    async def due(**kwargs):
        reads.append(kwargs)
        if not available:
            idle.set()
        return available

    async def drain():
        assert available
        calls.append(lane)
        handled.set()

    async def claim(**_kwargs):
        await drain()

    async def unused():
        raise AssertionError("an empty tick must not enter the consumer")

    monkeypatch.setattr(worker, "_STATE", {})
    monkeypatch.setattr(worker, "_wakeup_event", lambda: event)
    monkeypatch.setattr(worker, "_has_due_runtime_work", due)
    monkeypatch.setattr(worker, "claim_and_dispatch_once", claim)
    monkeypatch.setattr(
        worker,
        "_terminal_boundary_lanes",
        lambda _: {
            (lane if lane != "claim" else "web"): (drain if lane != "claim" else unused, poll),
        },
    )
    # Claim and the prompt consumer run independently. In the claim case the
    # web consumer has no work but is still invoked after a positive read.
    if lane == "claim":

        async def empty():
            pass

        monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda _: {"web": (empty, poll)})
    running = asyncio.create_task(
        worker.start_terminal_boundary_worker_loop(worker_id="a2q", claim_tasks=lane == "claim")
    )
    try:
        await asyncio.wait_for(idle.wait(), 1)
        await asyncio.sleep(0)
        assert calls == []
        available = True
        started = asyncio.get_running_loop().time()
        if wake:
            # This is the existing event set by notify_runtime_task_worker and
            # its Redis listener, not a new per-consumer wake channel.
            event.set()
        await asyncio.wait_for(handled.wait(), 0.1 if wake else poll + 0.03)
        assert asyncio.get_running_loop().time() - started < (0.1 if wake else poll + 0.03)
        assert calls == [lane]
        assert len(reads) >= 2
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("failure", ("raise", "hang"))
async def test_idle_read_failure_falls_back_to_existing_consumers(monkeypatch, failure):
    handled = asyncio.Event()

    async def due(**_kwargs):
        if failure == "raise":
            raise RuntimeError("synthetic idle read failure")
        await asyncio.Event().wait()

    async def drain():
        handled.set()

    monkeypatch.setattr(worker, "_STATE", {})
    monkeypatch.setattr(worker, "_wakeup_event", asyncio.Event)
    monkeypatch.setattr(worker, "_has_due_runtime_work", due)
    monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda _: {"web": (drain, 0.04)})
    running = asyncio.create_task(worker.start_terminal_boundary_worker_loop(worker_id="a2q"))
    try:
        await asyncio.wait_for(handled.wait(), 0.15)
        assert not running.done()
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_claim_and_consumer_do_not_block_each_other_or_duplicate_inflight_work(monkeypatch):
    claimed, drained, twice = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release = asyncio.Event()
    wakeup = asyncio.Event()
    claims = []

    async def due(**_kwargs):
        return True

    async def claim(**_kwargs):
        claims.append(1)
        claimed.set()
        if len(claims) == 1:
            await release.wait()
        else:
            twice.set()

    async def drain():
        drained.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "_STATE", {})
    monkeypatch.setattr(worker, "_wakeup_event", lambda: wakeup)
    monkeypatch.setattr(worker, "_has_due_runtime_work", due)
    monkeypatch.setattr(worker, "claim_and_dispatch_once", claim)
    monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda _: {"web": (drain, 60)})
    running = asyncio.create_task(worker.start_terminal_boundary_worker_loop(worker_id="a2q", claim_tasks=True))
    try:
        await asyncio.wait_for(asyncio.gather(claimed.wait(), drained.wait()), 1)
        wakeup.set()
        await asyncio.sleep(0.02)  # Let the wake's discovery run while claim is blocked.
        assert claims == [1]
        release.set()
        await asyncio.wait_for(twice.wait(), 0.1)
        assert claims == [1, 1]
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("lane", ("web", "claim"))
async def test_existing_redis_publish_wakes_idle_runtime_without_waiting_for_poll(monkeypatch, lane):
    from app.core.events import get_redis

    idle, handled = asyncio.Event(), asyncio.Event()
    available = False

    async def due(**_kwargs):
        if not available:
            idle.set()
        return available

    async def drain():
        if lane == "web":
            handled.set()

    async def claim(**_kwargs):
        if lane == "claim":
            handled.set()
        return []

    monkeypatch.setattr(worker, "_STATE", {})
    monkeypatch.setattr(worker, "_LOCAL_WAKEUP_EVENT", None)
    monkeypatch.setattr(worker, "_has_due_runtime_work", due)
    monkeypatch.setattr(worker, "claim_and_dispatch_once", claim)
    monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda _: {"web": (drain, 60)})
    running = asyncio.create_task(worker.start_runtime_task_worker_loop())
    redis = await get_redis()
    channel = worker._settings().RUNTIME_TASK_WAKEUP_CHANNEL
    try:
        await asyncio.wait_for(idle.wait(), 1)
        async with asyncio.timeout(1):
            while not any(count for _name, count in await redis.pubsub_numsub(channel)):
                await asyncio.sleep(0.005)
        available = True
        await redis.publish(channel, '{"reason":"a2q-local-test"}')
        await asyncio.wait_for(handled.wait(), 0.2)
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
