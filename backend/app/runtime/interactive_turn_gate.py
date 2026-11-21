"""Process-local scheduling for interactive turns and memory maintenance."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

BACKGROUND_WAIT_CAP_SECONDS = 15.0

_in_flight = 0
_idle = asyncio.Event()
_idle.set()


@asynccontextmanager
async def interactive_turn() -> AsyncIterator[None]:
    global _in_flight, _idle
    if not _in_flight:
        _idle = asyncio.Event()
    _in_flight += 1
    try:
        yield
    finally:
        _in_flight -= 1
        if not _in_flight:
            _idle.set()


# Continuous turns delay a chunk only up to its cap. Started chunks run
# uninterrupted. A separate worker process first needs a shared agent data
# volume because T2 writes package files there.
async def wait_for_no_interactive_turns(cap_seconds: float | None = None) -> float:
    """Wait for a quiet loop and return the elapsed wait in seconds."""
    cap = BACKGROUND_WAIT_CAP_SECONDS if cap_seconds is None else cap_seconds
    if not _in_flight or cap <= 0:
        return 0.0
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        async with asyncio.timeout(cap):
            while _in_flight:
                await _idle.wait()
    except TimeoutError:
        pass
    return loop.time() - started


@dataclass(slots=True)
class BackgroundWaitBudget:
    remaining_seconds: float

    async def wait(self) -> None:
        budgets = (self, *_shared_wait_budgets.get())
        cap = min(BACKGROUND_WAIT_CAP_SECONDS, *(budget.remaining_seconds for budget in budgets))
        elapsed = await wait_for_no_interactive_turns(cap)
        for budget in budgets:
            budget.remaining_seconds -= elapsed

    @contextmanager
    def share(self) -> Iterator[None]:
        """Charge nested T2 waits against the enclosing job or heartbeat too."""
        token = _shared_wait_budgets.set((*_shared_wait_budgets.get(), self))
        try:
            yield
        finally:
            _shared_wait_budgets.reset(token)


_shared_wait_budgets: ContextVar[tuple[BackgroundWaitBudget, ...]] = ContextVar(
    "background_wait_budgets", default=(),
)
