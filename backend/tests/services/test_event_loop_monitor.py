"""C2 — event loop lag monitor: quantifies L1 "is the loop blocked" directly."""

from __future__ import annotations

import asyncio
import time

import pytest


@pytest.mark.asyncio
async def test_monitor_samples_and_snapshot() -> None:
    from app.services.event_loop_monitor import EventLoopLagMonitor

    monitor = EventLoopLagMonitor(interval_seconds=0.01)
    monitor.start()
    try:
        await asyncio.sleep(0.08)
    finally:
        await monitor.stop()

    snap = monitor.snapshot()
    assert snap["samples"] >= 2
    assert snap["last_lag_ms"] >= 0.0
    assert snap["max_lag_ms"] >= 0.0
    assert snap["sample_interval_seconds"] == 0.01
    assert snap["running"] is False


@pytest.mark.asyncio
async def test_monitor_detects_blocking_work() -> None:
    from app.services.event_loop_monitor import EventLoopLagMonitor

    monitor = EventLoopLagMonitor(interval_seconds=0.01)
    monitor.start()
    try:
        await asyncio.sleep(0.02)
        time.sleep(0.1)  # deliberately block the loop, as sync file IO would
        await asyncio.sleep(0.03)
    finally:
        await monitor.stop()

    assert monitor.snapshot()["max_lag_ms"] >= 50.0


@pytest.mark.asyncio
async def test_monitor_start_idempotent_and_stop() -> None:
    from app.services.event_loop_monitor import EventLoopLagMonitor

    monitor = EventLoopLagMonitor(interval_seconds=0.01)
    monitor.start()
    monitor.start()
    assert monitor.snapshot()["running"] is True
    await monitor.stop()
    assert monitor.snapshot()["running"] is False
