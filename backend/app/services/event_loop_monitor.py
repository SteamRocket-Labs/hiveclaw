"""Event-loop lag monitor (C2 observability).

Samples how late a scheduled ``asyncio.sleep(interval)`` actually fires. Lag
persistently above zero means something is blocking the loop (sync file IO,
large JSON serialization). An off-loop sampler records code locations
while a heartbeat is delayed.
"""

from __future__ import annotations

import asyncio
import gc
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


def _log_stall(ms: float, frames: list[str], gc_ms: float) -> None:
    with _stall_log_lock:
        now = time.monotonic()
        while _stall_log_times and now - _stall_log_times[0] >= 60:
            _stall_log_times.popleft()
        if len(_stall_log_times) >= 20:
            return
        _stall_log_times.append(now)
    logger.warning("loop_stall %s", json.dumps({"ms": round(ms, 3), "frames": frames, "gc_ms": round(gc_ms, 3)}))


def _watch_loop_stalls(loop: asyncio.AbstractEventLoop, loop_thread: int, stop: threading.Event) -> None:
    heartbeat = threading.Event()
    heartbeat_pending = False
    answered_at = 0.0
    gc_total = 0.0
    gc_total_minus_start = None

    def acknowledge():
        nonlocal answered_at
        # Timestamp recovery on the loop, before the sampler wakes.
        answered_at = time.monotonic()
        heartbeat.set()

    def record_gc(phase: str, _info: dict[str, int]) -> None:
        nonlocal gc_total, gc_total_minus_start
        if phase == "start":
            gc_total_minus_start = gc_total - time.monotonic()
        elif gc_total_minus_start is not None:
            gc_total = gc_total_minus_start + time.monotonic()
            gc_total_minus_start = None

    def gc_seconds() -> float:
        # A collection that holds the GIL ends inside its "stop" callback, where the
        # sampler regains the GIL; count the collection still in progress.
        started = gc_total_minus_start
        return gc_total if started is None else started + time.monotonic()

    gc.callbacks.append(record_gc)
    try:
        while not stop.is_set():
            try:
                gc_before_wait = gc_seconds()
                slept_at = time.monotonic()
                if stop.wait(_HEARTBEAT_SECONDS) or not loop.is_running():
                    return
                overslept = time.monotonic() - slept_at - _HEARTBEAT_SECONDS
                if overslept >= _STALL_SECONDS:
                    frames = _stack_locations(sys._current_frames().get(loop_thread))
                    _log_stall(overslept * 1000, frames, (gc_seconds() - gc_before_wait) * 1000)
                    continue

                # Keep a single heartbeat pending, even when the loop cannot run it.
                if heartbeat_pending and not heartbeat.is_set():
                    continue
                heartbeat_pending = False
                heartbeat.clear()
                gc_before_wait = gc_seconds()
                sent_at = time.monotonic()
                loop.call_soon_threadsafe(acknowledge)
                heartbeat_pending = True
                frames = None
                if not heartbeat.wait(_STALL_SECONDS):
                    if stop.is_set() or not loop.is_running():
                        return
                    frames = _stack_locations(sys._current_frames().get(loop_thread))
                    while not heartbeat.wait(_HEARTBEAT_SECONDS):
                        if stop.is_set() or not loop.is_running():
                            return
                elapsed = answered_at - sent_at
                if elapsed >= _STALL_SECONDS and not stop.is_set():
                    if frames is None:
                        frames = _stack_locations(sys._current_frames().get(loop_thread))
                    _log_stall(elapsed * 1000, frames, (gc_seconds() - gc_before_wait) * 1000)
            except Exception:
                # The loop can close during sampling. Diagnostics must stay isolated.
                if stop.is_set():
                    return
                time.sleep(_HEARTBEAT_SECONDS)
    finally:
        gc.callbacks.remove(record_gc)


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
