"""Measure the live member turn against retained PostgreSQL and agent files."""

import asyncio
import functools
import inspect
import json
import logging
import sys
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine import Engine

from app.core import request_timing
from app.models.agent import Agent
from app.models.knowledge import KnowledgeDocument
from app.models.skill import Skill
from app.runtime import hooks, invocation_orchestrator, invoker
from app.services import agent_tools
from tests.integration import conftest as pg_fixtures
from tests.integration.test_a2fp1_private_turn_budget_postgres import (
    HelloTransport,
    LocalAuxTransport,
    Probe,
    app_user_engine,  # noqa: F401
    assert_committed_reply,
)
from tests.integration.test_a2p_member_request_cost_postgres import member_request  # noqa: F401
from tests.integration.test_m1f_assistant_binding_postgres import _agent
from tests.integration.test_sp1_send_statement_budget_postgres import claim, configure
from tests.integration.test_sp3b_run_timing_postgres import summaries

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
SEGMENTS = ("send", "pre", "post")
LAB_ROWS = {
    "agents": 69,
    "chat_sessions": 26774,
    "runtime_tasks": 530671,
    "chat_transcript_events": 202880,
    "invocation_spans": 121225,
    "session_model_results": 3671,
    "workspace_resource_manifests": 76,
    "knowledge_segments": 73,
    "skills": 9,
}
CEILINGS = {
    "warm": ((134, 13), (212, 36), (125, 14)),
    "second": ((129, 13), (218, 36), (125, 14)),
}
_METER_CONTEXT = ContextVar("volume_meter_context", default=None)


class VolumeProbe(Probe):
    @contextmanager
    def meter(self):
        """Count the measured request/run and its own jobs only."""
        identity = object()
        token = _METER_CONTEXT.set(identity)

        def active():
            return self.phase if _METER_CONTEXT.get() is identity else None

        def before(conn, _cursor, statement, _params, _context, _many):
            if phase := active():
                phase.sql[statement] += 1
                phase.roles.add(conn.engine.url.username)

        def begin(_conn):
            if phase := active():
                phase.transactions += 1

        def checkout(_connection, _record, _proxy):
            if phase := active():
                phase.checkouts += 1

        listeners = [("before_cursor_execute", before), ("begin", begin), ("checkout", checkout)]
        for name, callback in listeners:
            event.listen(Engine, name, callback)
        try:
            yield
        finally:
            self.switch()
            _METER_CONTEXT.reset(token)
            for name, callback in listeners:
                event.remove(Engine, name, callback)


async def seed_history(owner, seed):
    """Keep cardinalities on the same tenant and the same employee as the turn."""
    params = {"tenant": seed.tenant_id, "agent": seed.agent_id, "member": seed.user_id}
    async with owner() as db:
        agents = list(await db.scalars(select(Agent).where(Agent.tenant_id == seed.tenant_id)))
        model = next(agent.primary_model_id for agent in agents if agent.id == seed.agent_id)
        for i in range(len(agents), 69):
            await _agent(db, tenant_id=seed.tenant_id, owner_id=seed.user_id, name=f"Volume {i}", model_id=model)
        await db.flush()
        await db.execute(text("""
            INSERT INTO chat_sessions (id, tenant_id, agent_id, user_id, title, source_channel,
                session_kind, actor_type, runtime_source, visibility_scope, listed_surface, audience_kind)
            SELECT gen_random_uuid(), :tenant, :agent, :member, 'Retained private work', 'web',
                'human_chat', 'user', 'web_chat', 'direct_user', 'chat', 'private'
            FROM generate_series(1, 268 - (SELECT count(*) FROM chat_sessions WHERE agent_id = :agent))
        """), params)
        await db.execute(text("""
            WITH employees AS (
                SELECT array_agg(id ORDER BY id) AS ids FROM agents WHERE tenant_id = :tenant AND id <> :agent
            )
            INSERT INTO chat_sessions (id, tenant_id, agent_id, user_id, title, source_channel,
                session_kind, actor_type, runtime_source, visibility_scope, listed_surface, audience_kind)
            SELECT gen_random_uuid(), :tenant, ids[1 + (n % cardinality(ids))], :member,
                'Retained tenant work', 'web', 'human_chat', 'user', 'web_chat', 'direct_user', 'chat', 'private'
            FROM employees, generate_series(1, 26774 - (SELECT count(*) FROM chat_sessions WHERE tenant_id = :tenant)) n
        """), params)
        await db.execute(text("""
            CREATE TEMP TABLE volume_sessions ON COMMIT DROP AS
            SELECT id, agent_id, row_number() OVER (ORDER BY (agent_id = :agent) DESC, id) AS n
            FROM chat_sessions WHERE tenant_id = :tenant
        """), params)
        await db.execute(text("""
            INSERT INTO runtime_tasks (id, tenant_id, task_type, status, depth, parent_agent_id,
                child_agent_id, root_user_id, parent_session_id, child_session_id, root_session_id,
                created_at, completed_at, completion_outbox_generation, root_idempotency_key,
                config_snapshot_hash, policy_snapshot_hash)
            SELECT gen_random_uuid(), :tenant, 'heartbeat', CASE WHEN i <= 3671 THEN 'completed' ELSE 'skipped' END,
                1, s.agent_id, s.agent_id,
                :member, s.id::text, s.id::text, s.id::text, now() - interval '30 days',
                now() - interval '30 days', NULL, CAST(:tenant AS text) || '/volume-heartbeat/' || i,
                repeat('0', 64), repeat('0', 64)
            FROM generate_series(1, 530671 - (SELECT count(*) FROM runtime_tasks WHERE tenant_id = :tenant)) i
            JOIN volume_sessions s ON s.n = CASE WHEN i <=
                570 - (SELECT count(*) FROM runtime_tasks WHERE child_agent_id = :agent)
                THEN 1 + (i % 268) ELSE 269 + (i % (26774 - 268)) END
        """), params)
        await db.execute(text("""
            INSERT INTO chat_transcript_events (id, sequence, tenant_id, agent_id, session_id,
                actor_type, event_type, visibility_scope, listed_surface, content, projection_status, created_at)
            SELECT gen_random_uuid(), i + 1000, :tenant, s.agent_id, s.id, 'user', 'message',
                'direct_user', 'chat', 'Retained message', 'projected', now() - interval '30 days'
            FROM generate_series(1, 202880 - (SELECT count(*) FROM chat_transcript_events WHERE tenant_id = :tenant)) i
            JOIN volume_sessions s ON s.n = 1 + (i % 26774)
        """), params)
        await db.execute(text("""
            INSERT INTO invocation_spans (id, tenant_id, agent_id, user_id, session_id, span_id, trace_id,
                span_type, name, status, started_at, ended_at, duration_ms, evidence_refs, metadata_json,
                side_effect_refs, truth_evidence_json)
            SELECT gen_random_uuid(), :tenant, s.agent_id, :member, s.id::text, i::text, 'volume-history',
                'hook', 'hook.stop', 'ok', now() - interval '30 days', now() - interval '30 days', 0, '[]', '{}', '[]', '[]'
            FROM generate_series(1, 121225 - (SELECT count(*) FROM invocation_spans WHERE tenant_id = :tenant)) i
            JOIN volume_sessions s ON s.n = CASE WHEN i <=
                60920 - (SELECT count(*) FROM invocation_spans WHERE agent_id = :agent)
                THEN 1 + (i % 268) ELSE 269 + (i % (26774 - 268)) END
        """), params)
        await db.execute(text("""
            INSERT INTO session_model_results (id, tenant_id, session_id, run_id, turn_id, round_id,
                provider_request_id, state, model_request_hash, model_request_snapshot_json, bound_input_ids_json)
            SELECT gen_random_uuid(), :tenant, t.root_session_id::uuid, t.id,
                'turn-' || replace(t.id::text, '-', ''), 'volume-round',
                'volume-' || t.id, 'round_committed', 'history', '{}', '[]'
            FROM runtime_tasks t WHERE t.tenant_id = :tenant AND t.task_type = 'heartbeat' AND t.status = 'completed'
            LIMIT 3671 - (SELECT count(*) FROM session_model_results WHERE tenant_id = :tenant)
        """), params)
        await db.execute(text("""
            INSERT INTO workspace_resource_manifests (id, tenant_id, agent_id, path, owner_user_id,
                authority_state, source, root_session_id)
            SELECT gen_random_uuid(), :tenant, :agent, 'workspace/retained/report-' || s.n || '.md', :member,
                'owned', 'workspace_api', s.id
            FROM volume_sessions s WHERE s.n <= 76
        """), params)
        doc = KnowledgeDocument(tenant_id=seed.tenant_id, scope_id=seed.user_id, owner_user_id=seed.user_id,
            source_kind="upload", source_sha256="0" * 64, title="Volume fixture", status="ready",
            canonical_md_path="knowledge/volume.md")
        db.add(doc)
        await db.flush()
        await db.execute(text("""
            INSERT INTO knowledge_segments (id, tenant_id, document_id, scope_type, scope_id, position,
                segment_hash, heading_path_json, content, token_count, segment_metadata_json)
            SELECT gen_random_uuid(), :tenant, :document, 'person', :member, n, md5(n::text), '[]',
                'Retained knowledge', 3, '{}' FROM generate_series(1, 73) n
        """), {**params, "document": doc.id})
        for i in range(9):
            db.add(Skill(tenant_id=seed.tenant_id, name=f"Volume reference {i}", folder_name=f"volume-reference-{i}"))
        await db.commit()
    async with owner() as db:
        for table, expected in LAB_ROWS.items():
            count = await db.scalar(text(f"SELECT count(*) FROM {table} WHERE tenant_id = :tenant"), params)
            assert count == expected, (table, count, expected)
            await db.execute(text(f"ANALYZE {table}"))
        await db.commit()


def seed_files(root):
    """Bodies outside the prompt are sparse; the counted directories are real."""
    layouts = (
        ("runtime_artifacts/traces/{}.jsonl", 3541, 60_000_000),
        ("workspace/retained/report-{}.md", 159, 174_000_000),
        ("evolution/skill_candidates/{}/review.md", 200, 4_300_000),
        ("skills/references/volume-{}.md", 48, 280_000),
    )
    paths = []
    for index in range(3548):
        segment, member_file = divmod(index, 2)
        session, ordinal = segment % 268, segment // 268
        filename = ("events.jsonl", "source.md")[member_file]
        path = root / f"memory/t0/sessions/{session}/segments/{ordinal}/{filename}"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as stream:
            stream.truncate(1_300_000_000 // 3548)
        paths.append(path)
    for pattern, count, size in layouts:
        for index in range(1, count + 1):
            path = root / pattern.format(index)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as stream:
                stream.truncate(size // count)
            paths.append(path)
    sizes = [path.stat() for path in paths]
    print("A2FP2_FILES", len(paths), sum(s.st_size for s in sizes), sum(s.st_blocks * 512 for s in sizes), flush=True)


def watch_phases(monkeypatch, probe):
    recorded = defaultdict(lambda: {"statements": 0, "transactions": 0, "thread_cpu_ms": 0.0, "wall_ms": 0.0})

    def measure(name, function):
        def start():
            phase = probe.phase
            if phase is None or not phase.name.endswith(("-send", "-pre", "-post")):
                return None
            return phase, phase.sql.total(), phase.transactions, time.thread_time_ns(), time.perf_counter_ns()

        def finish(sample):
            if sample is None:
                return
            phase, sql, transactions, cpu, wall = sample
            metric = recorded[f"{phase.name}/{name}"]
            metric["statements"] += phase.sql.total() - sql
            metric["transactions"] += phase.transactions - transactions
            metric["thread_cpu_ms"] += (time.thread_time_ns() - cpu) / 1e6
            metric["wall_ms"] += (time.perf_counter_ns() - wall) / 1e6

        @functools.wraps(function)
        async def measured(*args, **kwargs):
            sample = start()
            try:
                return await function(*args, **kwargs)
            finally:
                finish(sample)

        return measured

    for module, attr, phase in (
        (invocation_orchestrator, "_build_skill_catalog", "skill_catalog"),
        (invoker, "_resolve_memory_context", "memory_context"),
        (invoker, "_build_system_prompt", "prompt_build"),
        (invoker, "_load_invocation_secret_boundary", "secret_boundary"),
    ):
        monkeypatch.setattr(module, attr, measure(phase, getattr(module, attr)))

    emit = hooks.emit_hook
    measured_emit = measure("hook_dispatch", emit)
    for module in list(sys.modules.values()):
        if getattr(module, "__name__", "").startswith("app.") and getattr(module, "emit_hook", None) is emit:
            monkeypatch.setattr(module, "emit_hook", measured_emit)
    return recorded


async def test_private_turn_volume_budget(member_request, owner_sessionmaker, monkeypatch, tmp_path, caplog):  # noqa: F811
    from app.core.events import close_redis
    from app.runtime.hooks_setup import register_memory_hooks
    from app.services import llm_client, runtime_task_worker as worker
    from app.services import web_chat_run_orchestrator as orchestrator, web_chat_runtime as runtime
    from app.services.web_terminal_boundary_processor import _SUMMARY_PROJECTION_TASKS

    _, _, _, factory = member_request
    seed, client = await configure(member_request, owner_sessionmaker, factory, monkeypatch, tmp_path)
    for name, function in vars(worker).copy().items():
        if inspect.iscoroutinefunction(function) and function.__module__ == worker.__name__:
            arg = inspect.signature(function).parameters.get("session_factory")
            if arg is not None and arg.default is not inspect.Parameter.empty:
                monkeypatch.setattr(worker, name, functools.partial(function, session_factory=factory))
    register_memory_hooks()
    monkeypatch.setattr(agent_tools, "_COLLECTED_TOOLS", None)
    monkeypatch.setattr(agent_tools, "_always_core_tools", None)
    monkeypatch.setattr(llm_client, "create_llm_client", lambda **_kwargs: LocalAuxTransport([]))
    caplog.set_level(logging.INFO, logger="app.core.request_timing")
    probe = VolumeProbe()
    diagnostics = watch_phases(monkeypatch, probe)
    original_execute = runtime.execute_web_chat_run
    trial = session_id = None

    def mark_terminal():
        request_timing.mark_run_terminal()
        if probe.phase is not None and probe.phase.name == f"{trial}-post":
            probe.phase.wall_ms = (time.perf_counter() - probe.phase.wall_start) * 1000
            probe.switch(f"{trial}-after")

    async def measured_execute(*args, **kwargs):
        probe.switch(f"{trial}-pre")
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "mark_run_terminal", mark_terminal)
    monkeypatch.setattr(runtime, "mark_run_terminal", mark_terminal)
    monkeypatch.setattr(runtime, "execute_web_chat_run", measured_execute)
    session_url = f"/api/agents/{seed.agent_id}/sessions"
    root = tmp_path / str(seed.agent_id)
    try:
        for volume in ("empty", "lab"):
            if volume == "lab":
                await seed_history(owner_sessionmaker, seed)
                seed_files(root)
            for turn in ("warmup", "warm", "second"):
                trial = f"{volume}-{turn}"
                provider = HelloTransport(trial, probe)
                monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
                url = f"{session_url}/{session_id}/runs" if turn == "second" else f"{session_url}/runs"
                with probe.meter():
                    probe.switch(f"{trial}-send")
                    response = await client.post(url, json={"content": "hello", "input_id": str(uuid.uuid4())})
                    probe.phase.wall_ms = (time.perf_counter() - probe.phase.wall_start) * 1000
                    probe.switch()
                assert response.status_code == 201, response.text
                body = response.json()
                if turn != "second":
                    session_id = uuid.UUID(body["session"]["id"])
                run_id = uuid.UUID((body if turn == "second" else body["run"])["run_id"])
                await claim(seed, run_id, owner_sessionmaker)
                with probe.meter():
                    assert runtime.dispatch_web_chat_run(run_id, claim_version=1, worker_id="rl0c")
                    await runtime._TASKS[run_id.hex]
                    assert probe.phase.name == f"{trial}-after"
                    assert len(provider.requests) == 1
                await assert_committed_reply(owner_sessionmaker, run_id, session_id, seed)
                timing, = [line for line in summaries(caplog) if line["run_id"] == str(run_id)]
                assert timing["rounds"] == 1 and timing["instrumentation_errors"] == 0
                probe.phases[f"{trial}-pre"].wall_ms = timing["pre_model_ms"]
                counts = await worker.drain_web_terminal_boundary_outbox_once(worker_id="volume-boundary")
                assert counts["delivered"] == 1, counts
                await asyncio.gather(*tuple(_SUMMARY_PROJECTION_TASKS))
    finally:
        await close_redis()

    measurements = {}
    for volume in ("empty", "lab"):
        for turn in CEILINGS:
            for segment in SEGMENTS:
                name = f"{volume}-{turn}-{segment}"
                phase = probe.phases[name]
                assert phase.roles <= {pg_fixtures.APP_USER}
                measurements[name] = phase.metrics() | {"wall_ms": round(phase.wall_ms, 3)}
                print("A2FP2_SEGMENT", name, json.dumps(measurements[name]), flush=True)
            predicted = sum(measurements[f"{volume}-{turn}-{segment}"]["predicted_ms"] for segment in SEGMENTS)
            print("A2FP2_TURN", f"{volume}-{turn}", f"predicted_ms={predicted:.3f}", flush=True)
    for name, metric in diagnostics.items():
        if "-warmup-" not in name:
            print("A2FP2_PHASE", name, json.dumps(metric), flush=True)

    exceeded = []
    for turn, budget in CEILINGS.items():
        for volume in ("empty", "lab"):
            total_sql = total_tx = 0
            for segment, (max_sql, max_tx) in zip(SEGMENTS, budget, strict=True):
                metric = measurements[f"{volume}-{turn}-{segment}"]
                sql, tx = metric["statements"], metric["transactions"]
                total_sql += sql
                total_tx += tx
                if sql > max_sql or tx > max_tx:
                    exceeded.append((volume, turn, segment, sql, tx, max_sql, max_tx))
                empty = measurements[f"empty-{turn}-{segment}"]
                if volume == "lab" and (sql, tx) != (empty["statements"], empty["transactions"]):
                    exceeded.append((volume, turn, segment, "volume grew", sql, tx, empty["statements"], empty["transactions"]))
            if total_sql > 250 or total_tx > 30:
                exceeded.append((volume, turn, "total", total_sql, total_tx, 250, 30))
    assert not exceeded, exceeded
