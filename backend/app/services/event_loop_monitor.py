"""Event-loop lag monitor (C2 observability).

Samples how late a scheduled ``asyncio.sleep(interval)`` actually fires. Lag
persistently above zero means something is blocking the loop (sync file IO,
large JSON serialization). An off-loop sampler records code locations
while a heartbeat is delayed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from collections import deque
from types import FrameType
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_INTERVAL_SECONDS = 5.0
_HEARTBEAT_SECONDS = 0.05
_STALL_SECONDS = 0.25
_MAX_FRAMES = 12
_stall_log_times: deque[float] = deque()
_stall_log_lock = threading.Lock()


def _stack_locations(frame: FrameType | None) -> list[str]:
    application = []
    library = None
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if module.startswith("app."):
            if len(application) < _MAX_FRAMES:
                application.append(f"{module}:{frame.f_code.co_name}:{frame.f_lineno}")
        elif library is None:
            library = f"{module}:{frame.f_code.co_name}:{frame.f_lineno}"
        if library is not None and len(application) >= _MAX_FRAMES - 1:
            break
        frame = frame.f_back
    return application if library is None else [*application[:_MAX_FRAMES - 1], library]


def _log_stall(ms: float, frames: list[str]) -> None:
    with _stall_log_lock:
        now = time.monotonic()
        while _stall_log_times and now - _stall_log_times[0] >= 60:
            _stall_log_times.popleft()
        if len(_stall_log_times) >= 20:
            return
        _stall_log_times.append(now)
    logger.warning("loop_stall %s", json.dumps({"ms": round(ms, 3), "frames": frames}))


def _watch_loop_stalls(loop: asyncio.AbstractEventLoop, loop_thread: int, stop: threading.Event) -> None:
    try:
        heartbeat = threading.Event()
        answered_at = 0.0

        def acknowledge():
            nonlocal answered_at
            # Timestamp recovery on the loop, before the sampler wakes.
            answered_at = time.monotonic()
            heartbeat.set()

        while not stop.wait(_HEARTBEAT_SECONDS) and loop.is_running():
            # Keep a single heartbeat pending, even when the loop cannot run it.
            heartbeat.clear()
            sent_at = time.monotonic()
            loop.call_soon_threadsafe(acknowledge)
            if heartbeat.wait(_STALL_SECONDS):
                continue
            if stop.is_set():
                return
            frames = _stack_locations(sys._current_frames().get(loop_thread))
            if heartbeat.is_set():
                continue
            while not heartbeat.wait(_HEARTBEAT_SECONDS):
                if stop.is_set() or not loop.is_running():
                    return
            if not stop.is_set():
                _log_stall((answered_at - sent_at) * 1000, frames)
    except Exception:
        # The loop can close during sampling. Diagnostics must stay isolated.
        return


def _start_loop_stall_sampler() -> threading.Event | None:
    try:
        stop = threading.Event()
        threading.Thread(
            target=_watch_loop_stalls,
            name="loop_stall_sampler",
            args=(asyncio.get_running_loop(), threading.get_ident(), stop),
            daemon=True,
        ).start()
        return stop
    except Exception:
        return None


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
        stop_sampler = _start_loop_stall_sampler()
        try:
            while True:
                started = time.perf_counter()
                await asyncio.sleep(self._interval_seconds)
                lag_seconds = (time.perf_counter() - started) - self._interval_seconds
                self._last_lag_ms = max(0.0, lag_seconds * 1000)
                self._max_lag_ms = max(self._max_lag_ms, self._last_lag_ms)
                self._samples += 1
        finally:
            if stop_sampler is not None:
                stop_sampler.set()

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self._task is not None and not self._task.done(),
            "sample_interval_seconds": self._interval_seconds,
            "samples": self._samples,
            "last_lag_ms": round(self._last_lag_ms, 2),
            "max_lag_ms": round(self._max_lag_ms, 2),
        }


event_loop_lag_monitor = EventLoopLagMonitor()
