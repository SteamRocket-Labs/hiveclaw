"""Per-daemon fanout concurrency caps (plan C1).

Heartbeat/trigger/dream tick dispatch was previously an unbounded
``asyncio.create_task`` per eligible agent — a batch of simultaneously
eligible agents could exhaust the shared DB connection pool. Each family
gets a semaphore sized from settings; excess dispatches queue behind it
instead of dropping.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from app.config import get_settings

_semaphores: dict[str, asyncio.Semaphore] = {}


def _limit_for(family: str) -> int:
    settings = get_settings()
    limits = {
        "heartbeat": settings.HEARTBEAT_MAX_CONCURRENT,
        "trigger": settings.TRIGGER_MAX_CONCURRENT,
        "dream": settings.DREAM_MAX_CONCURRENT,
    }
    return max(1, int(limits[family]))


def get_daemon_semaphore(family: str) -> asyncio.Semaphore:
    semaphore = _semaphores.get(family)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_limit_for(family))
        _semaphores[family] = semaphore
    return semaphore


def reset_daemon_semaphores_for_tests() -> None:
    _semaphores.clear()


async def run_bounded(family: str, awaitable: Awaitable[Any]) -> Any:
    """Await ``awaitable`` once a slot in the family's semaphore frees up."""
    async with get_daemon_semaphore(family):
        return await awaitable
