"""Behavioural regressions for the reviewed A2q coordinator, not symbol checks."""

from __future__ import annotations

import asyncio
import inspect
import math
from pathlib import Path
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
    assert task.cancelled()


async def test_positive_probe_does_not_spin_lanes_or_the_empty_claim_loop(schedule, monkeypatch):
    interval, duration = 0.1, 1.02
    calls = {"budget-transition": 0, "web": 0, "claim": 0}
    probes = []

    async def probe(**_kwargs):
        probes.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0.002)
        return {"budget-transition": True, "web": True}

    def drain(lane):
        async def tick():
            calls[lane] += 1
            await asyncio.sleep(0.003 if lane == "budget-transition" else 0.01)

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
        assert len(probes) <= limit, probes
        assert not running.done()
    finally:
        await _stop(running)


async def test_permanently_due_budget_lane_does_not_run_other_lanes(schedule, monkeypatch):
    interval, duration = 0.04, 0.42
    calls = {"budget-transition": 0, "web": 0}
    probes = []

    async def probe(**_kwargs):
        probes.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0.005)
        return {"budget-transition": True, "web": False}

    def drain(lane):
        async def tick():
            calls[lane] += 1
            await asyncio.sleep(0.008)

        return tick

    monkeypatch.setattr(worker, "_has_due_runtime_work", probe)
    monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda _: {lane: (drain(lane), interval) for lane in calls})
    running = asyncio.create_task(worker.start_terminal_boundary_worker_loop(worker_id="a2q-r2"))
    try:
        await asyncio.sleep(duration)
        limit = math.ceil(duration / interval) + 1
        assert 2 <= calls["budget-transition"] <= limit
        assert calls["web"] == 0
        assert len(probes) <= limit, probes
    finally:
        await _stop(running)


async def test_early_wake_waits_only_until_skipped_lane_is_due(schedule, monkeypatch):
    # Grok's 1 s probe / 1 s poll, scaled by 0.1. The wake is next_at,
    # not the claim Event: starting another probe must not delay this lane.
    interval = 0.1
    first, second = asyncio.Event(), asyncio.Event()
    times = []

    async def probe(**_kwargs):
        await asyncio.sleep(interval)
        return {"web": True}

    async def drain():
        times.append(asyncio.get_running_loop().time())
        (first if len(times) == 1 else second).set()

    monkeypatch.setattr(worker, "_has_due_runtime_work", probe)
    monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda _: {"web": (drain, interval)})
    running = asyncio.create_task(worker.start_terminal_boundary_worker_loop(worker_id="a2q-r2"))
    try:
        await asyncio.wait_for(first.wait(), 1)
        await asyncio.wait_for(second.wait(), interval + 0.035)
        assert interval - 0.005 <= times[1] - times[0] <= interval + 0.035, times
    finally:
        await _stop(running)


async def test_slow_probe_does_not_extend_sleep_after_a_slow_drain(schedule, monkeypatch):
    interval = 0.1  # probe 0.8 s / drain 1.5 s / poll 1 s, scaled by 0.1
    starts, finishes = [], []
    second = asyncio.Event()

    async def probe(**_kwargs):
        await asyncio.sleep(0.08)
        return {"web": True}

    async def drain():
        starts.append(asyncio.get_running_loop().time())
        if len(starts) == 2:
            second.set()
        await asyncio.sleep(0.15)
        finishes.append(asyncio.get_running_loop().time())

    monkeypatch.setattr(worker, "_has_due_runtime_work", probe)
    monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda _: {"web": (drain, interval)})
    running = asyncio.create_task(worker.start_terminal_boundary_worker_loop(worker_id="a2q-r3"))
    try:
        await asyncio.wait_for(second.wait(), 1)
        gap = starts[1] - finishes[0]
        assert interval - 0.005 <= gap <= interval + 0.025, (starts, finishes)
    finally:
        await _stop(running)


async def test_mixed_drain_deadlines_do_not_start_extra_probes(schedule, monkeypatch):
    interval, duration = 0.1, 0.72
    names = tuple(worker._runtime_work_candidates(now=worker._utcnow()))
    probes, calls = [], dict.fromkeys(names, 0)

    async def probe(**_kwargs):
        probes.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0.0055)
        return dict.fromkeys(names, True)

    def drain(lane, seconds):
        async def tick():
            calls[lane] += 1
            await asyncio.sleep(seconds)

        return tick

    monkeypatch.setattr(worker, "_has_due_runtime_work", probe)
    monkeypatch.setattr(
        worker,
        "_terminal_boundary_lanes",
        lambda _: {
            lane: (drain(lane, 0.001 + index * 0.034 / (len(names) - 1)), interval) for index, lane in enumerate(names)
        },
    )
    running = asyncio.create_task(worker.start_terminal_boundary_worker_loop(worker_id="a2q-r3"))
    try:
        await asyncio.sleep(duration)
        limit = math.ceil(duration / interval) + 1
        assert all(2 <= count <= limit for count in calls.values()), calls
        assert len(probes) <= limit, probes
        assert all(b - a >= interval - 0.003 for a, b in zip(probes, probes[1:])), probes
    finally:
        await _stop(running)


async def test_late_probe_false_is_discarded_for_unwoken_work(schedule, monkeypatch):
    interval = 0.2
    available = False
    handled = asyncio.Event()
    handled_at = []

    async def probe(**_kwargs):
        snapshot = available
        await asyncio.sleep(0.24)
        return {"web": snapshot}

    async def drain():
        if available:
            handled_at.append(asyncio.get_running_loop().time())
            handled.set()

    monkeypatch.setattr(worker, "_has_due_runtime_work", probe)
    monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda _: {"web": (drain, interval)})
    running = asyncio.create_task(worker.start_terminal_boundary_worker_loop(worker_id="a2q-r3"))
    try:
        await asyncio.sleep(0.302)
        became_due = asyncio.get_running_loop().time()
        available = True  # Deliberately no wake.
        await asyncio.wait_for(handled.wait(), interval + 0.035)
        assert handled_at[0] - became_due <= interval + 0.035
    finally:
        await _stop(running)


@pytest.mark.parametrize("lane", ("web", "unregistered-lane"))
async def test_missing_probe_key_fails_open(schedule, monkeypatch, lane):
    handled, probed = asyncio.Event(), asyncio.Event()
    false_leg_calls = []

    async def probe(**_kwargs):
        probed.set()
        return {"budget-transition": False}

    async def forbidden():
        false_leg_calls.append(1)
        pytest.fail("A present false leg must remain skipped")

    async def drain():
        handled.set()

    monkeypatch.setattr(worker, "_has_due_runtime_work", probe)
    monkeypatch.setattr(
        worker,
        "_terminal_boundary_lanes",
        lambda _: {lane: (drain, 0.04), "budget-transition": (forbidden, 0.04)},
    )
    running = asyncio.create_task(worker.start_terminal_boundary_worker_loop(worker_id="a2q-r3"))
    try:
        await asyncio.wait_for(probed.wait(), 0.1)
        await asyncio.wait_for(handled.wait(), 0.06)
        assert false_leg_calls == []
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


def test_replacement_candidate_is_in_the_reviewed_bypass_digest():
    from app.core.rls_bypass_manifest import _RLSBypassAnalyzer
    from app.services import session_turn_replacement

    name = "turn_replacement_candidate_statement"
    builder = getattr(worker, name, getattr(session_turn_replacement, name, None))
    source = Path(inspect.getsourcefile(builder)).resolve()
    app_root = Path(worker.__file__).resolve().parents[1]
    analyzer = _RLSBypassAnalyzer(app_root)
    entry = "\0".join((source.relative_to(app_root.parent).as_posix(), "<module-source>", source.read_text()))
    assert entry in analyzer._digest_entries, f"{source.name}: predicate changes escape the reviewed digest"


async def test_empty_consumer_map_exits_cleanly(schedule, monkeypatch):
    monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda _: {})
    await asyncio.wait_for(worker.start_terminal_boundary_worker_loop(worker_id="a2q-r2"), 0.1)
    assert worker._STATE["terminal_boundary_worker_running"] is False
