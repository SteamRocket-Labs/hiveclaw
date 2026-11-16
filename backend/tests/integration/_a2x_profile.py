"""Attribute the production request clock without storing SQL parameters."""

from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
import inspect
import json
import os
from pathlib import Path
import re
import sys

from httpx import ASGITransport, AsyncClient
from fastapi.routing import iter_route_contexts
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.sql import visitors
from sqlalchemy.sql.schema import Column, Table

from app.core.request_timing import install_cursor_timing, request_timing
from app.core.security import create_access_token

_PHASE = ContextVar("a2x_phase", default="authentication_and_middleware")
_POINT = ContextVar("a2x_checkpoint", default="request_entry")


@asynccontextmanager
async def permission_client(factory, seed, monkeypatch, restore_factory):
    import app.database as database
    from app.main import app

    # Use the real middleware, authentication and get_db dependency. Only the
    # physical request connection is redirected to the isolated runtime role.
    with monkeypatch.context() as patch:
        patch.setattr(database, "async_session", factory)
        token = create_access_token(str(seed.user_id), "member", tenant_id=str(seed.tenant_id))
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://a2x.test",
                headers={"Authorization": f"Bearer {token}"}) as client:
                yield client, app
        finally:
            restore_factory(factory)


def _application_stack():
    from greenlet import getcurrent

    current, frame, frames = getcurrent(), inspect.currentframe(), []
    while current is not None:
        while frame is not None:
            path = frame.f_code.co_filename
            if "/app/" in path:
                frames.append({"file": "app/" + path.split("/app/", 1)[1],
                    "function": frame.f_code.co_name, "line": frame.f_lineno})
            frame = frame.f_back
        current = current.parent
        frame = current.gr_frame if current is not None else None
    return frames


def _sql_columns(context, sql):
    from app.database import Base

    schema = Base.metadata.tables
    ctes = set(re.findall(r"(?:\bWITH(?:\s+RECURSIVE)?|,)\s*(\w+)(?:\([^)]*\))?\s+AS\s*\(", sql, re.I))
    names = set(schema) - ctes
    statement = getattr(getattr(context, "compiled", None), "statement", None)
    tables, columns = set(), set()
    if statement is not None:
        for node in visitors.iterate(statement):
            if isinstance(node, Table):
                tables.add(node.name)
            elif isinstance(node, Column) and node.table is not None:
                if node.table.name in names:
                    columns.add(f"{node.table.name}.{node.name}")
    tables.update(name for name in re.findall(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+(\w+)", sql, re.I)
        if name in names)
    aliases = {}
    for table, alias in re.findall(r"\b(?:FROM|JOIN)\s+(\w+)\s+(?:AS\s+)?(\w+)", sql, re.I):
        if table in names:
            aliases[alias] = table
    for alias, field in re.findall(r"\b(\w+)\.(\w+)\b", sql):
        table = aliases.get(alias, alias)
        if table in names and field in schema[table].columns:
            columns.add(f"{table}.{field}")
    inserted = re.search(r"\bINSERT\s+INTO\s+(\w+)\s*\(([^)]+)\)", sql, re.I)
    if inserted and inserted[1] in names:
        columns.update(f"{inserted[1]}.{field.strip()}" for field in inserted[2].split(","))
    updated = re.search(r"\bUPDATE\s+(\w+)\s+SET\s+(.*?)(?:\s+WHERE\b|\s+RETURNING\b|$)", sql, re.I | re.S)
    if updated and updated[1] in names:
        columns.update(f"{updated[1]}.{field}" for field in
            re.findall(r"(?:^|,)\s*(\w+)\s*=", updated[2]))
    return sorted(tables), sorted(columns)


@contextmanager
def request_statements(monkeypatch, app):
    from app.services import (
        agent_tools, credential_boundary_loader, delegation_permission_runtime, pack_policy_service,
        permission_execution, session_control_input, session_permission_runtime, session_tool_runtime,
        session_v2_persistence,
    )
    from app.api import chat_sessions

    sample = {"phases": Counter(), "sql": [], "route_body": None}
    invocations, reads, transactions = Counter(), {}, {}
    install_cursor_timing()

    def before(_connection, _cursor, sql, parameters, context, _many):
        timing = request_timing.get()
        if timing is None or not timing.active:
            return
        phase = _PHASE.get()
        if phase == "tool_checks" and sql.lstrip().startswith("INSERT INTO plaza_posts"):
            phase = "tool_effect"
        tables, columns = _sql_columns(context, sql)
        stack = _application_stack()
        point = _POINT.get()
        transaction = _connection.get_nested_transaction() or _connection.get_transaction()
        transaction_id = transactions.setdefault(transaction, len(transactions) + 1)
        row = {"number": len(sample["sql"]) + 1, "phase": phase, "checkpoint": point,
            "transaction": transaction_id, "tables": tables, "columns": columns, "sql": sql, "stack": stack}
        if sql.lstrip().startswith(("SELECT", "WITH")):
            # Values are compared only in this diagnostic scope. They are
            # never retained in the emitted inventory, even as hashes.
            key = point, transaction_id, sql, repr(parameters)
            row["identical_read_in_checkpoint"] = reads.get(key)
            reads[key] = row["number"]
        sample["sql"].append(row)
        sample["phases"][phase] += 1

    def wrap(patch, module, name, *, phase=None, point=None):
        original = getattr(module, name)
        label = point or f"{module.__name__}.{name}"

        async def measured(*args, **kwargs):
            invocations[label] += 1
            point_token = _POINT.set(f"{label}#{invocations[label]}")
            phase_token = _PHASE.set(phase) if phase is not None else None
            try:
                return await original(*args, **kwargs)
            finally:
                _POINT.reset(point_token)
                if phase_token is not None:
                    _PHASE.reset(phase_token)

        patch.setattr(module, name, measured)

    with monkeypatch.context() as patch:
        route = next(route for route in iter_route_contexts(app.routes) if route.path ==
            "/api/agents/{agent_id}/sessions/{session_id}/permissions/{permission_request_id}/resolve")
        original_route = route.dependant.call

        async def route_body(*args, **kwargs):
            timing = request_timing.get()
            started = timing.statements
            phase_token = _PHASE.set("execution_lease_relock_and_settlement")
            point_token = _POINT.set("resolve_route")
            try:
                return await original_route(*args, **kwargs)
            finally:
                sample["route_body"] = timing.statements - started
                _PHASE.reset(phase_token)
                _POINT.reset(point_token)

        patch.setattr(route.dependant, "call", route_body)
        wrap(patch, chat_sessions, "_get_run_session_and_agent", phase="route_authority", point="route.access")
        wrap(patch, session_v2_persistence, "resolve_session_mutation_authority",
            phase="route_authority", point="route.mutation_authority")
        for name in ("accept_tool_permission_response", "apply_permission_response_control_input"):
            wrap(patch, session_control_input, name, phase="permission_acceptance_and_control_input")
        wrap(patch, session_control_input, "_locked_permission_invocation")
        wrap(patch, agent_tools, "execute_session_permission_tool", phase="tool_checks")
        for module, name in (
            (delegation_permission_runtime, "validate_permission_input_sources"),
            (credential_boundary_loader, "load_exact_secret_boundary"),
            (pack_policy_service, "get_agent_pack_policies"),
            (session_tool_runtime, "mark_tool_effect_started"),
            (session_tool_runtime, "complete_tool_invocation"),
            (session_tool_runtime, "load_permission_plan_mode"),
            (session_permission_runtime, "_lock_permission_session"),
            (session_permission_runtime, "_source_result"),
            (permission_execution.PermissionExecutionLease, "ensure_owned"),
        ):
            wrap(patch, module, name)
        wrap(patch, session_permission_runtime, "_settle_unstarted_batch_siblings", phase="sibling_settlement")
        wrap(patch, session_permission_runtime, "_queue_same_run_continuation", phase="continuation")
        event.listen(Engine, "before_cursor_execute", before)
        try:
            yield sample
        finally:
            event.remove(Engine, "before_cursor_execute", before)


def save_inventory(name, observation, sample):
    from app.database import Base

    root = os.environ.get("FULL_RUN")
    if root is not None:
        label = sys.argv[1]
        assert re.fullmatch(r"[a-z0-9_]+", label)
        destination = Path(root) / "evidence" / f"{label}_{name}_inventory.json"
        tables = {table for row in sample["sql"] for table in row["tables"]}
        column_catalog = {table: list(Base.metadata.tables[table].columns.keys()) for table in sorted(tables)}
        destination.write_text(json.dumps({**observation, "column_catalog": column_catalog,
            "sql": sample["sql"]}, indent=2))
