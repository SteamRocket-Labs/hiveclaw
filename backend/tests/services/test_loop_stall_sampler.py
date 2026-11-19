"""Report synchronous stalls through the startup event-loop monitor."""

import asyncio
import json
import re
import threading
import time

import pytest

from app.services import event_loop_monitor
from app.services.event_loop_monitor import EventLoopLagMonitor


def _block_loop():
    time.sleep(0.4)


def _stall_samples(caplog):
    return [
        json.loads(message.removeprefix("loop_stall "))
        for message in caplog.messages
        if message.startswith("loop_stall ")
    ]


@pytest.mark.parametrize("blocked", [True, False])
async def test_monitor_reports_only_blocked_loop_stacks(caplog, blocked):
    monitor = EventLoopLagMonitor()
    monitor.start()
    try:
        await asyncio.sleep(0.075)
        if blocked:
            _block_loop()
        else:
            await asyncio.sleep(0.4)
        await asyncio.sleep(0.1)
    finally:
        await monitor.stop()

    samples = _stall_samples(caplog)
    assert len(samples) == int(blocked)
    if blocked:
        sample, = samples
        assert set(sample) == {"ms", "frames"}
        assert sample["ms"] >= 300
        assert len(sample["frames"]) <= 12
        assert any(f":{_block_loop.__name__}:" in location for location in sample["frames"])


async def test_monitor_reports_gil_holding_regex(caplog):
    size = 18
    while True:
        text = "a" * size + "b"
        started = time.perf_counter()
        re.match(r"(a+)+$", text)
        if time.perf_counter() - started >= 0.4:
            break
        size += 1

    monitor = EventLoopLagMonitor()
    monitor.start()
    try:
        await asyncio.sleep(0.075)
        re.match(r"(a+)+$", text)
        await asyncio.sleep(0.1)
    finally:
        await monitor.stop()

    sample, = _stall_samples(caplog)
    assert sample["ms"] >= 300
    assert any(location.startswith("re:match:") for location in sample["frames"])


async def test_sampler_keeps_running_after_capture_fails(caplog, monkeypatch):
    stack_locations = event_loop_monitor._stack_locations
    failed_capture = threading.Event()

    def fail_first_capture(frame):
        if not failed_capture.is_set():
            failed_capture.set()
            raise RuntimeError("synthetic stack capture failure")
        return stack_locations(frame)

    monkeypatch.setattr(event_loop_monitor, "_stack_locations", fail_first_capture)
    stop = threading.Event()
    sampler = threading.Thread(
        target=event_loop_monitor._watch_loop_stalls,
        args=(asyncio.get_running_loop(), threading.get_ident(), stop),
        daemon=True,
    )
    sampler.start()
    try:
        await asyncio.sleep(0.075)
        _block_loop()
        await asyncio.sleep(0.1)
        assert failed_capture.is_set()
        _block_loop()
        await asyncio.sleep(0.1)
        sample, = _stall_samples(caplog)
        assert any(f":{_block_loop.__name__}:" in location for location in sample["frames"])
        assert sampler.is_alive()
    finally:
        stop.set()
        sampler.join(timeout=1)
    assert not sampler.is_alive()
