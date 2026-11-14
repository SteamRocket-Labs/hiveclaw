"""Behavioural regressions for the reviewed A2q coordinator, not symbol checks."""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace

import pytest

from app.services import runtime_task_worker as worker


@pytest.fixture
def schedule(monkeypatch):
    event = asyncio.Event()
    monkeypatch.setattr(worker, "_STATE", {})
    monkeypatch.setattr(worker, "_wakeup_event", lambda: event)
    monkeypatch.setattr(
        worker,
        "_settings",
        lambda: SimpleNamespace(
            HIVE_PROCESS_ROLE="runtime",
            RUNTIME_TASK_WORKER_ENABLED=True,
            RUNTIME_TASK_WORKER_ID="a2q-r2",
            RUNTIME_TASK_CLAIM_POLL_SECONDS=0.1,
        ),
    )

    async def listener():
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "_redis_wakeup_listener", listener)
    return event


async def _stop(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_positive_probe_does_not_spin_lanes_or_the_empty_claim_loop(schedule, monkeypatch):
    interval, duration = 0.1, 1.02
    calls = {"budget-transition": 0, "web": 0, "claim": 0}

    async def probe(**_kwargs):
        await asyncio.sleep(0.002)
        return {"budget-transition": True, "web": True}

    def drain(lane):
        async def tick():
            calls[lane] += 1

        return tick

    async def claim(**_kwargs):
        calls["claim"] += 1
        return []

    monkeypatch.setattr(worker, "_has_due_runtime_work", probe)
    monkeypatch.setattr(worker, "claim_and_dispatch_once", claim)
    monkeypatch.setattr(
        worker,
        "_terminal_boundary_lanes",
        lambda _: {lane: (drain(lane), interval) for lane in ("budget-transition", "web")},
    )
    running = asyncio.create_task(worker.start_runtime_task_worker_loop())
    try:
        await asyncio.sleep(duration)
        limit = math.ceil(duration / interval) + 1
        assert all(2 <= count <= limit for count in calls.values()), calls
        assert not running.done()
    finally:
        await _stop(running)


async def test_permanently_due_budget_lane_does_not_run_other_lanes(schedule, monkeypatch):
    calls = {"budget-transition": 0, "web": 0}

    async def probe(**_kwargs):
        return {"budget-transition": True, "web": False}

    def drain(lane):
        async def tick():
            calls[lane] += 1

        return tick

    monkeypatch.setattr(worker, "_has_due_runtime_work", probe)
    monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda _: {lane: (drain(lane), 0.04) for lane in calls})
    running = asyncio.create_task(worker.start_terminal_boundary_worker_loop(worker_id="a2q-r2"))
    try:
        await asyncio.sleep(0.15)
        assert calls["budget-transition"] >= 2
        assert calls["web"] == 0
    finally:
        await _stop(running)


async def test_early_wake_waits_only_until_skipped_lane_is_due(schedule, monkeypatch):
    interval = 0.2
    first, second = asyncio.Event(), asyncio.Event()
    times = []

    async def probe(**_kwargs):
        return {"web": True}

    async def drain():
        times.append(asyncio.get_running_loop().time())
        (first if len(times) == 1 else second).set()

    monkeypatch.setattr(worker, "_has_due_runtime_work", probe)
    monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda _: {"web": (drain, interval)})
    running = asyncio.create_task(worker.start_terminal_boundary_worker_loop(worker_id="a2q-r2"))
    try:
        await asyncio.wait_for(first.wait(), 1)
        await asyncio.sleep(max(0, times[0] + interval - 0.01 - asyncio.get_running_loop().time()))
        schedule.set()
        await asyncio.wait_for(second.wait(), 0.07)
        assert times[1] - times[0] <= interval + 0.035, times
    finally:
        await _stop(running)


@pytest.mark.parametrize("failure", ("error", "hang", "foreign-cancel"))
async def test_probe_failure_keeps_claims_and_lanes_running_without_cancelling_late_read(
    schedule, monkeypatch, failure
):
    release = asyncio.Event()
    probes, cancelled, claims, lanes = [], [], [], []

    async def probe(**_kwargs):
        probes.append(1)
        if failure == "error":
            raise RuntimeError("synthetic probe failure")
        if failure == "foreign-cancel":
            raise asyncio.CancelledError("foreign probe cancellation")
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(1)
            await release.wait()  # Cancellation cannot complete until the read returns.
        return {"web": False}

    async def claim(**_kwargs):
        claims.append(1)
        return []

    async def drain():
        lanes.append(1)

    monkeypatch.setattr(worker, "_has_due_runtime_work", probe)
    monkeypatch.setattr(worker, "claim_and_dispatch_once", claim)
    monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda _: {"web": (drain, 0.04)})
    running = asyncio.create_task(worker.start_runtime_task_worker_loop())
    try:
        await asyncio.sleep(0.27)
        assert not running.done()
        assert 2 <= len(claims) <= 4
        assert len(lanes) >= 2
        if failure == "hang":
            assert len(probes) == 1
            assert cancelled == []
    finally:
        release.set()
        await asyncio.sleep(0)
        await _stop(running)


async def test_woken_claim_never_waits_for_a_probe(schedule, monkeypatch):
    entered, release, first, second = (asyncio.Event() for _ in range(4))
    calls = []

    async def probe(**_kwargs):
        entered.set()
        await release.wait()
        return {"web": False}

    async def claim(**_kwargs):
        calls.append(1)
        (first if len(calls) == 1 else second).set()
        return []

    async def drain():
        pass

    monkeypatch.setattr(worker, "_has_due_runtime_work", probe)
    monkeypatch.setattr(worker, "claim_and_dispatch_once", claim)
    monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda _: {"web": (drain, 60)})
    running = asyncio.create_task(worker.start_runtime_task_worker_loop())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(first.wait(), 0.03)
        schedule.set()
        await asyncio.wait_for(second.wait(), 0.03)
        assert len(calls) == 2 and not release.is_set()
    finally:
        release.set()
        await _stop(running)


async def test_skipped_lane_does_not_forge_success_or_clear_its_error(schedule, monkeypatch):
    class Readiness(dict):
        def __bool__(self):
            return any(self.values())

    state = {"last_success_at": "old", "last_error": "previous failure", "error_type": "ValueError"}
    monkeypatch.setattr(worker, "_STATE", {"lanes": {"web": dict(state)}})

    async def probe(**_kwargs):
        return Readiness(web=False)

    async def forbidden():
        pytest.fail("A false leg must not run its consumer")

    monkeypatch.setattr(worker, "_has_due_runtime_work", probe)
    monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda _: {"web": (forbidden, 0.02)})
    running = asyncio.create_task(worker.start_terminal_boundary_worker_loop(worker_id="a2q-r2"))
    try:
        await asyncio.sleep(0.05)
        assert worker._STATE["lanes"]["web"] == state
    finally:
        await _stop(running)


async def test_slow_consumer_still_sleeps_its_interval_after_finishing(schedule, monkeypatch):
    starts, finishes = [], []
    interval = 0.04

    async def probe(**_kwargs):
        return {"web": True}

    async def drain():
        starts.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0.07)
        finishes.append(asyncio.get_running_loop().time())

    monkeypatch.setattr(worker, "_has_due_runtime_work", probe)
    monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda _: {"web": (drain, interval)})
    running = asyncio.create_task(worker.start_terminal_boundary_worker_loop(worker_id="a2q-r2"))
    try:
        await asyncio.sleep(0.2)
        assert len(starts) >= 2
        assert all(
            starts[i + 1] - finished >= interval - 0.008 for i, finished in enumerate(finishes[: len(starts) - 1])
        )
    finally:
        await _stop(running)


async def test_empty_consumer_map_exits_cleanly(schedule, monkeypatch):
    monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda _: {})
    await asyncio.wait_for(worker.start_terminal_boundary_worker_loop(worker_id="a2q-r2"), 0.1)
    assert worker._STATE["terminal_boundary_worker_running"] is False
