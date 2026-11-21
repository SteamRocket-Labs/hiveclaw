"""Process-local scheduling for interactive turns and memory maintenance."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

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


# Under continuous multi-user load, each background job yields to turns for at
# most its budget from when it starts, then runs alongside turns.
# Started chunks run uninterrupted. A separate worker process first needs a
# shared agent data volume because T2 writes package files there.
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


class BackgroundWaitBudget:
    __slots__ = ("_deadline",)

    def __init__(self, seconds: float) -> None:
        self._deadline = asyncio.get_running_loop().time() + seconds

    async def wait(self) -> None:
        budgets = (self, *_shared_wait_budgets.get())
        deadline = min(budget._deadline for budget in budgets)
        cap = min(BACKGROUND_WAIT_CAP_SECONDS, deadline - asyncio.get_running_loop().time())
        if cap > 0:
            await wait_for_no_interactive_turns(cap)

    @contextmanager
    def share(self) -> Iterator[None]:
        """Apply this job or heartbeat's deadline to nested T2 waits."""
        token = _shared_wait_budgets.set((*_shared_wait_budgets.get(), self))
        try:
            yield
        finally:
            _shared_wait_budgets.reset(token)


_shared_wait_budgets: ContextVar[tuple[BackgroundWaitBudget, ...]] = ContextVar(
    "background_wait_budgets", default=(),
)
