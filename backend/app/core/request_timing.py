"""Request and run measurements sharing the same cursor/checkout listeners."""

import hashlib
import heapq
import json
import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from inspect import iscoroutinefunction
from time import perf_counter, thread_time

from greenlet import getcurrent
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

logger = logging.getLogger(__name__)


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


@dataclass
class _PhaseTiming:
    wall_seconds: float = 0.0
    count: int = 0
    statements: int = 0
    transactions: int = 0
    db_seconds: float = 0.0
    pool_seconds: float = 0.0
    cpu_seconds: float = 0.0


@dataclass
class RunTiming(RequestTiming):
    run_id: str = ""
    task_type: str = "web_chat_turn"
    started: float = field(default_factory=perf_counter)
    started_cpu: float = field(default_factory=thread_time)
    terminal_at: float | None = None
    terminal_cpu_at: float | None = None
    first_provider_at: float | None = None
    first_provider_cpu_at: float | None = None
    last_provider_end: float | None = None
    last_provider_cpu_end: float | None = None
    model_seconds: float = 0.0
    between_seconds: float = 0.0
    rounds: int = 0
    transactions: int = 0
    instrumentation_errors: int = 0
    slow_statements: list = field(default_factory=list)
    slow_gaps: list = field(default_factory=list)
    phases: dict[str, _PhaseTiming] = field(default_factory=dict)

    def summary(self, finished: float) -> dict:
        end = self.terminal_at or finished
        first = self.first_provider_at if self.first_provider_at is not None else end
        finished_cpu = thread_time()
        cpu_end = self.terminal_cpu_at if self.terminal_cpu_at is not None else finished_cpu
        cpu_first = self.first_provider_cpu_at if self.first_provider_cpu_at is not None else cpu_end
        return {
            "run_id": self.run_id, "task_type": self.task_type,
            "wall_ms": round((end - self.started) * 1000, 3),
            "cleanup_ms": round((finished - end) * 1000, 3),
            "cpu_ms": round((finished_cpu - self.started_cpu) * 1000, 3),
            "pre_model_ms": round((first - self.started) * 1000, 3),
            "pre_model_cpu_ms": round((cpu_first - self.started_cpu) * 1000, 3),
            "model_ms": round(self.model_seconds * 1000, 3),
            "between_rounds_ms": round(self.between_seconds * 1000, 3),
            "post_model_ms": round((end - self.last_provider_end) * 1000, 3)
                if self.last_provider_end is not None else 0.0,
            "post_model_cpu_ms": round((cpu_end - self.last_provider_cpu_end) * 1000, 3)
                if self.last_provider_cpu_end is not None else 0.0,
            "rounds": self.rounds, "db_statements": self.statements,
            "db_ms": round(self.db_seconds * 1000, 3),
            "pool_ms": round(self.pool_seconds * 1000, 3), "transactions": self.transactions,
            "instrumentation_errors": self.instrumentation_errors,
            "statements": [{"hash": sql_hash, "frame": frame, "ms": round(seconds * 1000, 3)}
                for seconds, _, sql_hash, frame in sorted(self.slow_statements, reverse=True)],
            "gaps": [{"from": previous, "to": frame, "ms": round(seconds * 1000, 3)}
                for seconds, _, previous, frame in sorted(self.slow_gaps, reverse=True)],
            "phases": [
                {"name": name, "ms": round(phase.wall_seconds * 1000, 3), "n": phase.count,
                    "statements": phase.statements, "transactions": phase.transactions,
                    "db_ms": round(phase.db_seconds * 1000, 3), "pool_ms": round(phase.pool_seconds * 1000, 3),
                    "cpu_ms": round(phase.cpu_seconds * 1000, 3)}
                for name, phase in sorted(self.phases.items(), key=lambda item: item[1].wall_seconds, reverse=True)[:10]
            ],
        }


def _run_timing() -> RunTiming | None:
    timing = request_timing.get()
    return timing if isinstance(timing, RunTiming) and timing.active else None


def _observe(timing, operation, *args):
    """Keep failures local to optional measurement, including frame/hash failures."""
    try:
        return operation(*args)
    except Exception:
        timing.instrumentation_errors += 1
        return None


def _application_frame() -> str:
    current, frame = getcurrent(), sys._getframe(1)
    while current is not None:
        while frame is not None:
            module = frame.f_globals.get("__name__", "")
            if module.startswith("app.") and module != __name__:
                return f"{module}:{frame.f_code.co_name}:{frame.f_lineno}"
            frame = frame.f_back
        # SQLAlchemy's async bridge puts the application await in the parent
        # greenlet. Walk its existing frames without building a stack snapshot.
        current = current.parent
        frame = current.gr_frame if current is not None else None
    return "unknown"


def _top_ten(entries, entry):
    if len(entries) < 10:
        heapq.heappush(entries, entry)
    else:
        heapq.heappushpop(entries, entry)


@contextmanager
def run_timing_scope(run_id, *, task_type="web_chat_turn"):
    timing, token = None, None
    try:
        timing = RunTiming(run_id=str(run_id), task_type=task_type)
        token = request_timing.set(timing)
        _observe(timing, install_cursor_timing)
    except Exception:
        pass
    try:
        yield timing
    finally:
        if timing is not None:
            timing.active = False
            _observe(timing, _emit_run_timing, timing, perf_counter())
        if token is not None:
            request_timing.reset(token)


def _emit_run_timing(timing, finished):
    logger.info("run_timing %s", json.dumps(timing.summary(finished), separators=(",", ":")))


def timed_web_chat_run(function):
    @wraps(function)
    async def measured(run_id, *args, **kwargs):
        with run_timing_scope(run_id):
            return await function(run_id, *args, **kwargs)
    return measured


def mark_run_terminal() -> None:
    timing = _run_timing()
    if timing is not None and timing.terminal_at is None:
        timing.terminal_at = perf_counter()
        timing.terminal_cpu_at = _observe(timing, thread_time)


@contextmanager
def provider_request_timing():
    timing, started = _run_timing(), None
    if timing is not None:
        started = _observe(timing, _provider_started, timing)
    try:
        yield
    finally:
        if started is not None and timing.active:
            _observe(timing, _provider_finished, timing, started)


def _provider_started(timing):
    started = perf_counter()
    if timing.first_provider_at is None:
        timing.first_provider_cpu_at = thread_time()
        timing.first_provider_at = started
    if timing.last_provider_end is not None:
        timing.between_seconds += started - timing.last_provider_end
    timing.rounds += 1
    return started


def _provider_finished(timing, started):
    timing.last_provider_end, timing.last_provider_cpu_end = perf_counter(), thread_time()
    timing.model_seconds += timing.last_provider_end - started


def timed_phase(name):
    def decorate(function):
        def start(timing):
            # The shared counters include nested work. Thread time also includes
            # any other tasks that run on this event-loop thread while awaiting.
            return (perf_counter(), thread_time(), timing.statements, timing.transactions,
                timing.db_seconds, timing.pool_seconds)

        def finish(timing, started):
            wall_at, cpu_at, statements, transactions, db_seconds, pool_seconds = started
            wall_seconds = perf_counter() - wall_at
            cpu_seconds = thread_time() - cpu_at
            phase = timing.phases.get(name)
            if phase is None:
                phase = timing.phases[name] = _PhaseTiming()
            phase.wall_seconds += wall_seconds
            phase.count += 1
            phase.statements += timing.statements - statements
            phase.transactions += timing.transactions - transactions
            phase.db_seconds += timing.db_seconds - db_seconds
            phase.pool_seconds += timing.pool_seconds - pool_seconds
            phase.cpu_seconds += cpu_seconds

        @wraps(function)
        async def measured_async(*args, **kwargs):
            timing = _run_timing()
            if timing is None:
                return await function(*args, **kwargs)
            started = _observe(timing, start, timing)
            try:
                return await function(*args, **kwargs)
            finally:
                if started is not None and timing.active:
                    _observe(timing, finish, timing, started)

        @wraps(function)
        def measured_sync(*args, **kwargs):
            timing = _run_timing()
            if timing is None:
                return function(*args, **kwargs)
            started = _observe(timing, start, timing)
            try:
                return function(*args, **kwargs)
            finally:
                if started is not None and timing.active:
                    _observe(timing, finish, timing, started)

        return measured_async if iscoroutinefunction(function) else measured_sync
    return decorate


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
        attribution = None
        if isinstance(timing, RunTiming):
            attribution = _observe(timing, _statement_attribution, timing, _connection, _statement)
        context._hive_request_timing = timing, perf_counter(), attribution


def _statement_attribution(timing, connection, statement):
    frame = _application_frame()
    # Match PostgreSQL's activity-query truncation at a complete UTF-8
    # character, as used by the production sampler (1023 bytes).
    prefix = statement[:1023].encode("utf-8")[:1023].decode("utf-8", errors="ignore").encode("utf-8")
    sql_hash = hashlib.md5(prefix).hexdigest()[:12]
    transaction = connection.info.get("_hive_run_transaction")
    if transaction is not None and transaction[0] is timing and transaction[1] is not None:
        ended, previous = transaction[1]
        _top_ten(timing.slow_gaps, (perf_counter() - ended, timing.statements, previous, frame))
    return connection, sql_hash, frame


def _finish_cursor(context) -> None:
    started = getattr(context, "_hive_request_timing", None)
    if started is not None:
        timing, clock, attribution = started
        if timing.active:
            ended = perf_counter()
            timing.db_seconds += ended - clock
            if isinstance(timing, RunTiming) and attribution is not None:
                _observe(timing, _finish_attribution, timing, clock, ended, attribution)
        context._hive_request_timing = None


def _finish_attribution(timing, started, ended, attribution):
    connection, sql_hash, frame = attribution
    _top_ten(timing.slow_statements, (ended - started, timing.statements, sql_hash, frame))
    transaction = connection.info.get("_hive_run_transaction")
    if transaction is not None and transaction[0] is timing:
        connection.info["_hive_run_transaction"] = timing, (ended, frame)


def _cursor_finished(_connection, _cursor, _statement, _parameters, context, _many):
    _finish_cursor(context)


def _cursor_failed(context):
    _finish_cursor(context.execution_context)


def _transaction_started(connection):
    timing = _run_timing()
    if timing is not None:
        _observe(timing, _begin_transaction, timing, connection)


def _begin_transaction(timing, connection):
    timing.transactions += 1
    connection.info["_hive_run_transaction"] = timing, None


def _transaction_ended(connection):
    timing = _run_timing()
    if timing is not None:
        _observe(timing, _finish_transaction, timing, connection)


def _finish_transaction(timing, connection):
    transaction = connection.info.get("_hive_run_transaction")
    try:
        if transaction is not None and transaction[0] is timing and transaction[1] is not None:
            ended, previous = transaction[1]
            _top_ten(timing.slow_gaps,
                (perf_counter() - ended, timing.statements, previous, _application_frame()))
    finally:
        connection.info.pop("_hive_run_transaction", None)


def install_cursor_timing() -> None:
    # Middleware construction follows database initialization, so the cursor
    # clock starts after the existing parameter-repair ingress listener.
    if not event.contains(Engine, "before_cursor_execute", _cursor_started):
        event.listen(Engine, "before_cursor_execute", _cursor_started)
        event.listen(Engine, "after_cursor_execute", _cursor_finished)
        event.listen(Engine, "handle_error", _cursor_failed)
        event.listen(Engine, "begin", _transaction_started)
        event.listen(Engine, "commit", _transaction_ended)
        event.listen(Engine, "rollback", _transaction_ended)
