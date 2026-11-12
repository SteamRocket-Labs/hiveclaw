"""Request-only cursor and connection-checkout measurements."""

from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import AsyncAdaptedQueuePool


@dataclass
class RequestTiming:
    db_seconds: float = 0.0
    pool_seconds: float = 0.0
    statements: int = 0
    active: bool = True

    def header(self, app_seconds: float) -> str:
        return (f'app;dur={app_seconds * 1000:.1f}, '
            f'db;dur={self.db_seconds * 1000:.1f};desc="{self.statements}", '
            f'pool;dur={self.pool_seconds * 1000:.1f}')


request_timing: ContextVar[RequestTiming | None] = ContextVar("request_timing", default=None)


class TimedAsyncAdaptedQueuePool(AsyncAdaptedQueuePool):
    """Time the native checkout call, including its queue, connect and pre-ping."""

    def connect(self):
        timing = request_timing.get()
        if timing is None or not timing.active:
            return super().connect()
        start = perf_counter()
        try:
            return super().connect()
        finally:
            if timing.active:
                timing.pool_seconds += perf_counter() - start


def _cursor_started(_connection, _cursor, _statement, _parameters, context, _many):
    timing = request_timing.get()
    if timing is not None and timing.active:
        timing.statements += 1
        context._hive_request_timing = timing, perf_counter()


def _finish_cursor(context) -> None:
    started = getattr(context, "_hive_request_timing", None)
    if started is not None:
        timing, clock = started
        if timing.active:
            timing.db_seconds += perf_counter() - clock
        context._hive_request_timing = None


def _cursor_finished(_connection, _cursor, _statement, _parameters, context, _many):
    _finish_cursor(context)


def _cursor_failed(context):
    _finish_cursor(context.execution_context)


def install_cursor_timing() -> None:
    # Middleware construction follows database initialization, so the cursor
    # clock starts after the existing parameter-repair ingress listener.
    if not event.contains(Engine, "before_cursor_execute", _cursor_started):
        event.listen(Engine, "before_cursor_execute", _cursor_started)
        event.listen(Engine, "after_cursor_execute", _cursor_finished)
        event.listen(Engine, "handle_error", _cursor_failed)
