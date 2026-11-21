"""Attribute the existing SQL samples to memory-tool stages and checkpoints."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import time

from sqlalchemy import event
from sqlalchemy.engine import Engine

STAGE = ContextVar("a2w_memory_measurement_stage", default="tool")


def _phase(value):
    if isinstance(value, tuple):
        return [_phase(part) for part in value]
    return value if value is None or isinstance(value, (str, int, bool)) else "walk"


@contextmanager
def memory_phase_profile(monkeypatch):
    from app.services import content_input_receipts, explicit_memory_authority, explicit_memory_tool_sources
    from app.services import team_memory_runtime
    from app.tools.handlers import memory
    from tests.integration.test_r15e_artifact_graph_reads_postgres import _SQL_SOURCE

    totals = {}

    def before(_conn, _cursor, _statement, _parameters, context, _many):
        scope = content_input_receipts._DISCLOSURE_SOURCE_VALIDATIONS.get()
        checkpoint = scope.checkpoint if scope is not None else (None, None)
        context.a2w_memory_phase = (STAGE.get(), _SQL_SOURCE.get(), checkpoint[0], json.dumps(_phase(checkpoint[1])))

    def after(_conn, _cursor, _statement, _parameters, context, _many):
        row = totals.setdefault(context.a2w_memory_phase, {"statements": 0, "sql_seconds": 0., "injected_seconds": 0.})
        row["statements"] += 1
        row["sql_seconds"] += time.perf_counter() - context.r15e[0]
        row["injected_seconds"] += context.r15e_injected

    def install(patch, module, name, stage, *, inherit=False):
        original = getattr(module, name)

        @wraps(original)
        async def measured(*args, **kwargs):
            current = STAGE.get()
            token = STAGE.set(current if inherit and current != "tool" else stage)
            try:
                return await original(*args, **kwargs)
            finally:
                STAGE.reset(token)

        patch.setattr(module, name, measured)

    with monkeypatch.context() as patch:
        install(patch, memory, "_scoped_explicit_entries", "admission")
        install(patch, explicit_memory_authority, "authorized_explicit_entries", "selection", inherit=True)
        install(patch, explicit_memory_tool_sources, "record_memory_result", "record")
        install(patch, team_memory_runtime, "read_entries", "team")
        install(patch, memory, "search_session_history", "legacy")
        # SM1: governed recall is never model input (R1); the scan is skipped.
        install(patch, memory, "has_authorized_past_session", "legacy")
        event.listen(Engine, "before_cursor_execute", before)
        event.listen(Engine, "after_cursor_execute", after)
        try:
            yield totals
        finally:
            event.remove(Engine, "before_cursor_execute", before)
            event.remove(Engine, "after_cursor_execute", after)
