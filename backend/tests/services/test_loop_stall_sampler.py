"""Report synchronous stalls through the startup event-loop monitor."""

import asyncio
import json
import time

import pytest

from app.services.event_loop_monitor import EventLoopLagMonitor


def _block_loop():
    time.sleep(0.4)


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

    samples = [
        json.loads(message.removeprefix("loop_stall "))
        for message in caplog.messages
        if message.startswith("loop_stall ")
    ]
    assert len(samples) == int(blocked)
    if blocked:
        sample, = samples
        assert set(sample) == {"ms", "frames"}
        assert sample["ms"] >= 300
        assert len(sample["frames"]) <= 12
        assert any(f":{_block_loop.__name__}:" in location for location in sample["frames"])
