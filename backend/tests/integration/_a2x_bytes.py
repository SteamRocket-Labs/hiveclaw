"""Measure received JSONB value bytes, keeping the driver's real decoder."""

from contextlib import contextmanager

import asyncpg
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.core.request_timing import request_timing


class _ReceivedJSON(dict):
    pass


@contextmanager
def snapshot_wire_bytes(monkeypatch):
    active = {}
    sample = {"snapshot_jsonb_bytes": 0, "snapshot_cells": 0, "reads": []}
    original_codec = asyncpg.Connection.set_type_codec

    async def codec(connection, typename, **kwargs):
        decoder = kwargs.get("decoder")
        if typename == "jsonb" and decoder is not None:
            def received(value):
                result = decoder(value)
                if connection in active and isinstance(result, dict):
                    result = _ReceivedJSON(result)
                    result.received_bytes = len(value)
                return result
            kwargs = {**kwargs, "decoder": received}
        return await original_codec(connection, typename, **kwargs)

    def before(_connection, cursor, sql, _parameters, _context, _many):
        timing = request_timing.get()
        if timing is not None and timing.active and sql.lstrip().startswith("SELECT") \
            and "session_model_results.model_request_snapshot_json" in sql:
            active[cursor._connection] = sql

    def after(_connection, cursor, _sql, _parameters, _context, _many):
        sql = active.pop(cursor._connection, None)
        if sql is None:
            return
        columns = [column[0] for column in cursor.description or ()]
        if "model_request_snapshot_json" not in columns:
            return
        index = columns.index("model_request_snapshot_json")
        sizes = []
        for row in cursor._rows:
            value = row[index]
            if value is not None:
                assert isinstance(value, _ReceivedJSON), "The physical connection must use the measured codec"
                sizes.append(value.received_bytes)
        sample["snapshot_jsonb_bytes"] += sum(sizes)
        sample["snapshot_cells"] += len(sizes)
        sample["reads"].append({"bytes": sum(sizes), "cells": len(sizes)})

    with monkeypatch.context() as patch:
        patch.setattr(asyncpg.Connection, "set_type_codec", codec)
        event.listen(Engine, "before_cursor_execute", before)
        event.listen(Engine, "after_cursor_execute", after)
        try:
            yield sample
        finally:
            event.remove(Engine, "before_cursor_execute", before)
            event.remove(Engine, "after_cursor_execute", after)


def full_model_rows(patch):
    """Diagnostic counterfactual: retain batching/reuse, disable only Model projections."""
    from sqlalchemy.orm import load_only
    from app.models.session_v2 import SessionModelResult
    from app.services import session_permission_runtime, session_tool_runtime

    def columns(*attributes):
        if attributes[0].class_ is SessionModelResult:
            return load_only(*(getattr(SessionModelResult, column.key)
                for column in SessionModelResult.__mapper__.column_attrs))
        return load_only(*attributes)

    for module in (session_permission_runtime, session_tool_runtime):
        if hasattr(module, "load_only"):
            patch.setattr(module, "load_only", columns)
