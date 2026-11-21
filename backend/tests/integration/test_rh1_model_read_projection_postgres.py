"""Measure whole request bytes at the PostgreSQL cursor on two model read paths."""

import json
import time
from contextlib import contextmanager

import pytest
from sqlalchemy import Text, cast, event, func, select
from sqlalchemy.engine import Engine

from app.memory.t2 import segment_package as t2
from app.models.session_v2 import SessionModelResult
from app.services import memory_service, session_model_round
from tests.integration import test_m1g30_model_dependencies_postgres as model_fixtures
from tests.integration.test_a2fp1_private_turn_budget_postgres import Probe, app_user_engine  # noqa: F401
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_mf1_memory_shape_postgres import mf1_memory_shape  # noqa: F401
from tests.integration.test_t2_source_revocation_postgres import _model, read_turn  # noqa: F401


@pytest.fixture(autouse=True)
def large_model_requests(monkeypatch):
    prepare = session_model_round.prepare_model_request
    tool = {"type": "function", "function": {"name": "synthetic_schema", "parameters": {"type": "object"},
        "description": "Synthetic input material. " * 6_000}}

    async def prepare_with_large_schema(db, **kwargs):
        return await prepare(db, **{**kwargs, "tools": [*kwargs["tools"], tool]})

    # Both fixtures write through the normal request and seal code.
    monkeypatch.setattr(session_model_round, "prepare_model_request", prepare_with_large_schema)
    monkeypatch.setattr(model_fixtures, "prepare_model_request", prepare_with_large_schema)


async def _assert_large_requests(owner_sessionmaker, session_id, minimum_rows):
    async with owner_sessionmaker() as db:
        sizes = list(await db.scalars(select(func.octet_length(cast(SessionModelResult.model_request_snapshot_json, Text)))
            .where(SessionModelResult.session_id == session_id,
                   SessionModelResult.state.in_(("sealed", "round_committed")))))
    assert len(sizes) >= minimum_rows and min(sizes) >= 280_000, sizes


@contextmanager
def _measure(phase):
    cursor_results = []

    def collect(_conn, cursor, statement, _params, _context, _many):
        if cursor.description and "model_request_snapshot_json" in statement:
            names = tuple(column[0] for column in cursor.description)
            cursor_results.extend((names, row) for row in cursor._rows)

    probe, metrics = Probe(), {}
    event.listen(Engine, "after_cursor_execute", collect)
    try:
        with probe.meter():
            probe.switch(phase)
            started = time.perf_counter()
            yield metrics
            wall_ms = (time.perf_counter() - started) * 1000
    finally:
        event.remove(Engine, "after_cursor_execute", collect)

    # asyncpg decodes JSONB before exposing the cursor rows. Count the UTF-8
    # bytes of those values, including snapshots nested in a catalogue row.
    whole_bytes = 0
    for names, row in cursor_results:
        for name, value in zip(names, row, strict=True):
            snapshot = value if name == "model_request_snapshot_json" else (
                value.get("model_request_snapshot_json") if isinstance(value, dict) else None)
            if snapshot is not None:
                whole_bytes += len(json.dumps(snapshot, ensure_ascii=False).encode())
    metrics.update(probe.phases[phase].metrics(), wall_ms=round(wall_ms, 3), whole_snapshot_bytes=whole_bytes)
    print("RH1_READ", phase, json.dumps(metrics, sort_keys=True), flush=True)


async def test_warm_private_memory_context_returns_no_whole_model_requests(mf1_memory_shape, owner_sessionmaker):  # noqa: F811
    n, agent, expected = mf1_memory_shape
    await _assert_large_requests(owner_sessionmaker, n.session, minimum_rows=9)

    async def build():
        return await memory_service.build_memory_context(n.agent, n.tenant, session_id=str(n.session),
            runtime_task_id=str(n.run), current_user_id=n.user, verified_agent=agent, return_result=True)

    warm = await build()
    assert {ref["source_id"] for ref in warm.source_refs if ref["source_kind"] == "explicit_memory"} == expected
    assert warm.degraded_components == ()
    with _measure("memory_context") as metrics:
        measured = await build()
    assert measured == warm
    assert metrics["whole_snapshot_bytes"] == 0, metrics


async def test_t2_job_returns_no_whole_model_requests(read_turn, owner_sessionmaker, monkeypatch):  # noqa: F811
    s = read_turn
    await _assert_large_requests(owner_sessionmaker, s.n.session, minimum_rows=2)
    calls, _ = _model(monkeypatch)

    with _measure("t2_job") as metrics:
        result = await t2.run_t2_segment_package_job(agent_id=s.n.agent, tenant_id=s.n.tenant,
            session_id=s.n.session, t0_segment_id=s.segment)

    assert result.status == "committed", result.issues
    assert len(calls) == 3 and s.body in json.dumps(calls[0])
    assert metrics["whole_snapshot_bytes"] == 0, metrics
