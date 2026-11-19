"""Sample loop activity through the startup event-loop monitor."""

import asyncio
import json
import sys
import time
from types import FunctionType, ModuleType

import pytest

from app.services import event_loop_monitor


def _spin():
    deadline = time.perf_counter() + 1.0
    while time.perf_counter() < deadline:
        pass


@pytest.mark.parametrize("loop_kind", ["asyncio", "uvloop"])
@pytest.mark.parametrize("busy", [True, False], ids=["busy", "idle"])
def test_loop_profile_sampler(caplog, monkeypatch, loop_kind, busy):
    window_seconds = 1.25
    monkeypatch.setattr(event_loop_monitor, "_PROFILE_WINDOW_SECONDS", window_seconds, raising=False)
    loop_factory = asyncio.new_event_loop if loop_kind == "asyncio" else pytest.importorskip("uvloop").new_event_loop

    module = ModuleType("app.loop_profile_test")
    module.time = time
    monkeypatch.setitem(sys.modules, module.__name__, module)
    spin = FunctionType(_spin.__code__, vars(module))

    async def run():
        monitor = event_loop_monitor.EventLoopLagMonitor()
        finish_at = time.monotonic() + window_seconds + 0.15
        monitor.start()
        try:
            if busy:
                await asyncio.sleep(0.075)
                spin()
            await asyncio.sleep(max(0.0, finish_at - time.monotonic()))
        finally:
            await monitor.stop()

    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(run())

    profiles = [
        json.loads(message.removeprefix("loop_profile "))
        for message in caplog.messages
        if message.startswith("loop_profile ")
    ]
    if not busy:
        assert all(profile["busy_ms"] < profile["window_ms"] * 0.1 for profile in profiles)
        return

    profile, = profiles
    assert set(profile) == {"window_ms", "busy_ms", "gc_ms", "entries", "leaves"}
    assert profile["busy_ms"] >= 600
    assert profile["entries"][0][0] == profile["leaves"][0][0] == f"{module.__name__}:_spin"
