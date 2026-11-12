"""Reproducible CPU profiles for the real member request fixtures."""

import cProfile
import json
import os
from pathlib import Path
import pstats
import re
import time
import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.middleware import TraceIdMiddleware
from app.core.request_timing import request_timing
from app.config import get_settings
from app.database import _engine_pool_kwargs
from tests.integration.conftest import (  # noqa: F401
    app_user_engine, app_user_sessionmaker, owner_engine, owner_sessionmaker, migrated_pg_url,
)
from tests.integration.test_a2p_member_request_cost_postgres import member_request  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

def _short(path):
    if "/app/" in path:
        return "app/" + path.split("/app/", 1)[1]
    if "/site-packages/" in path:
        return path.split("/site-packages/", 1)[1]
    return path


@pytest.mark.parametrize("endpoint", ["create_run", "upload", "assistant"])
async def test_non_database_profile(member_request, app_user_engine, monkeypatch, tmp_path, endpoint):  # noqa: F811
    import app.database as database

    seed, client, _sample, _factory = member_request
    engine = create_async_engine(app_user_engine.url, **_engine_pool_kwargs(get_settings()))
    monkeypatch.setattr(database, "async_session", async_sessionmaker(engine, expire_on_commit=False))
    async with engine.connect():
        pass
    original = client._transport.app
    app = original.app
    app.add_middleware(TraceIdMiddleware)
    app.middleware_stack = app.build_middleware_stack()
    cpu = cProfile.Profile(timer=time.process_time)
    stages, collecting = [], False

    def state():
        value = request_timing.get()
        return (value.db_seconds, value.pool_seconds) if value is not None else (0, 0)

    def record(name, function, started, before):
        after = state()
        db_ms, pool_ms = ((after[i] - before[i]) * 1000 for i in range(2))
        wall_ms = (time.perf_counter() - started) * 1000
        stages.append({"stage": name, "file": _short(function.__code__.co_filename),
            "line": function.__code__.co_firstlineno, "wall_ms": round(wall_ms, 3),
            "db_ms": round(db_ms, 3), "pool_ms": round(pool_ms, 3),
            "rest_ms": round(wall_ms - db_ms - pool_ms, 3)})

    def observe(owner, attribute, name, *, asynchronous):
        function = getattr(owner, attribute)

        async def async_call(*args, **kwargs):
            active, started, before = collecting, time.perf_counter(), state()
            result = await function(*args, **kwargs)
            if active:
                record(name, function, started, before)
            return result

        def sync_call(*args, **kwargs):
            active, started, before = collecting, time.perf_counter(), state()
            result = function(*args, **kwargs)
            if active:
                record(name, function, started, before)
            return result

        monkeypatch.setattr(owner, attribute, async_call if asynchronous else sync_call)

    from app.api import chat_sessions, upload
    from app.services import assistant_binding, credential_boundary_loader, source_read_queries, upload_originals
    from app.services.document_conversion import DocumentConversionService
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    for owner, attribute, name in (
        (chat_sessions, "submit_live_human_input", "admission_dispatch"),
        (credential_boundary_loader, "redact_runtime_ingress_payload", "credential_boundary"),
        (upload_originals, "archive_upload", "original_archive"),
        (PersonalKnowledgeService, "queue_source_bytes_import", "knowledge_enqueue"),
        (assistant_binding, "_candidates", "candidates"),
        (assistant_binding, "_model_readiness", "model_readiness"),
        (assistant_binding, "_continuation_session", "continuation_session"),
    ):
        observe(owner, attribute, name, asynchronous=True)
    for owner, attribute, name in (
        (upload, "_write_upload_copy", "file_write"),
        (DocumentConversionService, "convert", "conversion"),
        (source_read_queries, "source_reader_statement", "acl_sql_build"),
    ):
        observe(owner, attribute, name, asynchronous=False)

    class ToHeaders:
        async def __call__(self, scope, receive, send):
            nonlocal collecting
            async def observed(message):
                nonlocal collecting
                if message["type"] == "http.response.start":
                    collecting = False
                    cpu.disable()
                await send(message)
            collecting = True
            cpu.enable()
            try:
                await original(scope, receive, observed)
            finally:
                collecting = False
                cpu.disable()

    client._transport.app = ToHeaders()

    def rtt(_connection, _cursor, _statement, _parameters, _context, _many):
        until = time.perf_counter() + 0.00235
        while time.perf_counter() < until:
            pass

    event.listen(Engine, "before_cursor_execute", rtt)
    try:
        if endpoint == "create_run":
            response = await client.post(f"/api/agents/{seed.agent_id}/sessions/runs",
                json={"content": "Summarize the sales report", "input_id": str(uuid.uuid4())})
        elif endpoint == "upload":
            response = await client.post("/api/chat/upload", data={"agent_id": str(seed.agent_id)},
                files={"file": ("sales.csv", b"region,sales\n" + b"West,42\n" * 89, "text/csv")})
        else:
            response = await client.get("/api/assistant")
    finally:
        event.remove(Engine, "before_cursor_execute", rtt)
        await engine.dispose()
    assert response.status_code == (201 if endpoint == "create_run" else 200), response.text
    if endpoint == "upload":
        body = response.json()
        assert body["size"] == 725 and body["conversion"]["status"] == "converted"
        assert body["personal_kb_candidate"]["status"] == "queued"
    header = response.headers["server-timing"]
    parsed = re.fullmatch(r'app;dur=([\d.]+), db;dur=([\d.]+);desc="(\d+)", pool;dur=([\d.]+)', header)
    assert parsed, header
    app_ms, db_ms, statements, pool_ms = parsed.groups()
    stats = pstats.Stats(cpu)
    costs = [{"file": _short(file), "line": line, "function": function,
        "calls": count, "self_cpu_ms": round(self_time * 1000, 3), "cumulative_cpu_ms": round(cumulative * 1000, 3)}
        for (file, line, function), (_primitive, count, self_time, cumulative, _callers) in stats.stats.items()]
    directory = Path(os.environ.get("FULL_RUN", str(tmp_path))) / "evidence" / make_url(get_settings().DATABASE_URL).database
    directory.mkdir(parents=True, exist_ok=True)
    cpu.dump_stats(str(directory / f"{endpoint}.pstats"))
    (directory / f"{endpoint}.json").write_text(json.dumps(costs))
    (directory / f"{endpoint}-stages.json").write_text(json.dumps(stages))
    top = sorted([row for row in costs if row["file"].startswith("app/")
        and row["file"] != "app/core/request_timing.py"], key=lambda row: -row["self_cpu_ms"])[:12]
    print("A2P_CPU", json.dumps({"endpoint": endpoint, "header": header,
        "app_ms": float(app_ms), "db_ms": float(db_ms), "pool_ms": float(pool_ms),
        "rest_ms": round(float(app_ms) - float(db_ms) - float(pool_ms), 1), "statements": int(statements),
        "process_cpu_ms": round(stats.total_tt * 1000, 3), "stages": stages, "top_app_self": top}), flush=True)
