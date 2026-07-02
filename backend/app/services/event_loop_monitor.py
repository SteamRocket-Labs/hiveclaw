"""Event-loop lag monitor (C2 observability).

Samples how late a scheduled ``asyncio.sleep(interval)`` actually fires. Lag
persistently above zero means something is blocking the loop (sync file IO,
large JSON serialization) — the L1 attribution instrument from
docs/performance-slimming-plan-2026-07-02.md.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

DEFAULT_SAMPLE_INTERVAL_SECONDS = 5.0


class EventLoopLagMonitor:
    def __init__(self, interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS) -> None:
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._last_lag_ms = 0.0
        self._max_lag_ms = 0.0
        self._samples = 0

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self._run(), name="event_loop_lag_monitor")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            started = time.perf_counter()
            await asyncio.sleep(self._interval_seconds)
            lag_seconds = (time.perf_counter() - started) - self._interval_seconds
            self._last_lag_ms = max(0.0, lag_seconds * 1000)
            self._max_lag_ms = max(self._max_lag_ms, self._last_lag_ms)
            self._samples += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self._task is not None and not self._task.done(),
            "sample_interval_seconds": self._interval_seconds,
            "samples": self._samples,
            "last_lag_ms": round(self._last_lag_ms, 2),
            "max_lag_ms": round(self._max_lag_ms, 2),
        }


event_loop_lag_monitor = EventLoopLagMonitor()
