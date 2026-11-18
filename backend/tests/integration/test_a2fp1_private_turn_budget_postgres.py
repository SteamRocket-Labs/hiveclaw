"""Measure the native member send and claimed one-round text reply on PostgreSQL."""

import asyncio
import functools
import inspect
import json
import logging
import time
import uuid
from collections import Counter
from contextlib import contextmanager

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import request_timing
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from app.runtime import invoker
from app.services import agent_tools
from tests.integration import conftest as pg_fixtures
from tests.integration.test_a2p_member_request_cost_postgres import member_request  # noqa: F401
from tests.integration.test_sp1_send_statement_budget_postgres import claim, configure
from tests.integration.test_sp3b_run_timing_postgres import summaries
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
ANSWER = "Hello!"
TAIL_SECONDS = 20.0
CEILINGS = {
    "warm": {"send": (135, 13), "pre": (224, 39), "post": (125, 14)},
    "second": {"send": (130, 13), "pre": (230, 39), "post": (125, 14)},
}


@pytest.fixture
async def app_user_engine(migrated_pg_url):
    """Same SP4c non-owner credentials with the production pool and serialization."""
    from app import database

    url = make_url(migrated_pg_url).set(
        username=pg_fixtures.APP_USER, password=pg_fixtures.APP_USER_PASSWORD,
    )
    engine = create_async_engine(
        url,
        connect_args=database._engine_connect_args(database.settings),
        json_serializer=database._postgres_json_serializer,
        **database._engine_pool_kwargs(database.settings),
    )
    try:
        yield engine
    finally:
        await engine.dispose()


class Phase:
    def __init__(self, name):
        self.name = name
        self.sql = Counter()
        self.roles = set()
        self.transactions = 0
        self.checkouts = 0
        self.wall_start = time.perf_counter()
        self.cpu_start = time.thread_time_ns()

    def stop(self):
        self.cpu_ms = (time.thread_time_ns() - self.cpu_start) / 1e6

    def metrics(self):
        statements = self.sql.total()
        round_trips = statements + 3 * self.transactions
        return {
            "statements": statements,
            "distinct_sql": len(self.sql),
            "transactions": self.transactions,
            "checkouts": self.checkouts,
            "thread_cpu_ms": round(self.cpu_ms, 3),
            "round_trips": round_trips,
            "predicted_ms": round(1.6 * self.cpu_ms + 2.5 * round_trips, 3),
        }


class Probe:
    def __init__(self):
        self.phase = None
        self.phases = {}

    def switch(self, name=None):
        if self.phase is not None:
            self.phase.stop()
        self.phase = Phase(name) if name else None
        if self.phase is not None:
            self.phases[name] = self.phase

    @contextmanager
    def meter(self):
        def before(conn, _cursor, statement, _params, _context, _many):
            if self.phase is not None:
                self.phase.sql[statement] += 1
                self.phase.roles.add(conn.engine.url.username)

        def begin(_conn):
            if self.phase is not None:
                self.phase.transactions += 1

        def checkout(_connection, _record, _proxy):
            if self.phase is not None:
                self.phase.checkouts += 1

        listeners = [("before_cursor_execute", before), ("begin", begin), ("checkout", checkout)]
        for name, callback in listeners:
            event.listen(Engine, name, callback)
        try:
            yield
        finally:
            self.switch()
            for name, callback in listeners:
                event.remove(Engine, name, callback)


class HelloTransport(ScriptedLLMClient):
    def __init__(self, trial, probe):
        super().__init__([{"content": ANSWER, "stream_text": ANSWER}])
        self.trial = trial
        self.probe = probe

    async def stream(self, **kwargs):
        assert self.probe.phase.name == f"{self.trial}-pre"
        self.probe.switch(f"{self.trial}-transport")
        try:
            return await super().stream(**kwargs)
        finally:
            self.probe.switch(f"{self.trial}-post")


class LocalAuxTransport:
    """Replace only auxiliary provider replies, keeping app-side metering and projections."""

    def __init__(self, calls):
        self.calls = calls

    async def stream(self, *, messages, **_kwargs):
        from app.services.conversation_summarizer import _SUMMARIZE_SYSTEM_PROMPT
        from app.services.session_memory import _SESSION_MEMORY_WRITER_PROMPT

        system = messages[0].content
        if system == _SUMMARIZE_SYSTEM_PROMPT:
            return self._answer("session_summary", "<summary>The member greeted the agent.</summary>")
        if system == _SESSION_MEMORY_WRITER_PROMPT:
            return self._answer("session_memory_writer", '{"current_state":"The member greeted the agent."}')
        raise AssertionError("Unexpected auxiliary streaming transport request")

    async def complete(self, *, messages, **_kwargs):
        system = messages[0].content
        if "Choose signal_type" in system:
            return self._answer("fast_reflection", '{"signal_type":"low_signal","container":"none"}')

        from app.memory.t2 import segment_package
        from tests.memory.test_t2_job_sweep import _approved_review_xml, _labels_xml, _summary_xml

        payload = json.loads(messages[1].content)
        if system == segment_package.SUMMARY_AGENT_PROMPT:
            return self._answer("t2_summary", _summary_xml(payload))
        if system == segment_package.LEARNING_BRAIN_LABELS_PROMPT:
            return self._answer("t2_labels", _labels_xml(payload["source_bundle"]))
        if system == segment_package.MEMORY_GATE_REVIEW_PROMPT:
            bundle = payload["source_bundle"]
            return self._answer("t2_review", _approved_review_xml(
                package_id=bundle["package_id"], ref=bundle["source_refs"][0]["uri"],
            ))
        raise AssertionError("Unexpected auxiliary completion transport request")

    def _answer(self, kind, content):
        from app.services.llm_client import LLMResponse

        self.calls.append(kind)
        return LLMResponse(content=content, finish_reason="stop", usage={"prompt_tokens": 10, "completion_tokens": 5})

    async def close(self):
        pass


async def assert_committed_reply(owner_sessionmaker, run_id, session_id, seed):
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        session = await db.get(ChatSession, session_id)
        result = (await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id))).one()
        reply = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.run_id == run_id,
            ChatTranscriptEvent.item_kind == "assistant_final",
        ))
        assert task.status == "completed"
        assert session.audience_kind in {None, "private"} and session.visibility_scope == "direct_user"
        assert session.source_channel == "web" and session.user_id == seed.user_id and session.agent_id == seed.agent_id
        assert result.state == "round_committed" and ANSWER in json.dumps(result.seal_json)
        assert reply is not None and reply.result_id == result.id and reply.lifecycle == "completed"


async def test_private_turn_budget(member_request, owner_sessionmaker, monkeypatch, tmp_path, caplog):  # noqa: F811
    from app.core.events import close_redis
    from app.runtime.hooks_setup import register_memory_hooks
    from app.services import llm_client, runtime_control_bus, runtime_task_worker as worker
    from app.services import web_chat_run_orchestrator as orchestrator, web_chat_runtime as runtime
    from app.services.runtime_budget_daemon import start_runtime_budget_daemon
    from app.services.web_chat_stream_bus import start_web_chat_stream_forwarder

    _, _, _, factory = member_request
    seed, client = await configure(member_request, owner_sessionmaker, factory, monkeypatch, tmp_path)
    # Worker entry points with captured defaults must use the same non-owner role.
    for name, function in vars(worker).copy().items():
        if inspect.iscoroutinefunction(function) and function.__module__ == worker.__name__:
            factory_arg = inspect.signature(function).parameters.get("session_factory")
            if factory_arg is not None and factory_arg.default is not inspect.Parameter.empty:
                monkeypatch.setattr(worker, name, functools.partial(function, session_factory=factory))

    register_memory_hooks()
    monkeypatch.setattr(agent_tools, "_COLLECTED_TOOLS", None)
    monkeypatch.setattr(agent_tools, "_always_core_tools", None)
    caplog.set_level(logging.INFO, logger="app.core.request_timing")
    aux_calls = []
    monkeypatch.setattr(llm_client, "create_llm_client", lambda **_kwargs: LocalAuxTransport(aux_calls))
    probe = Probe()
    original_execute = runtime.execute_web_chat_run
    sessions_url = f"/api/agents/{seed.agent_id}/sessions"
    session_id = None
    session_ids = []
    trial = None

    def mark_terminal():
        request_timing.mark_run_terminal()
        if probe.phase is not None and probe.phase.name == f"{trial}-post":
            probe.switch(f"{trial}-after")

    async def measured_execute(*args, **kwargs):
        probe.switch(f"{trial}-pre")
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "mark_run_terminal", mark_terminal)
    monkeypatch.setattr(runtime, "mark_run_terminal", mark_terminal)
    monkeypatch.setattr(runtime, "execute_web_chat_run", measured_execute)
    try:
        for trial in ("cold", "warm", "second"):
            provider = HelloTransport(trial, probe)
            monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
            url = f"{sessions_url}/{session_id}/runs" if trial == "second" else f"{sessions_url}/runs"
            with probe.meter():
                probe.switch(f"{trial}-send")
                response = await client.post(url, json={"content": "hello", "input_id": str(uuid.uuid4())})
                probe.switch()
                assert response.status_code == 201, response.text
                body = response.json()
                if trial != "second":
                    session_id = uuid.UUID(body["session"]["id"])
                session_ids.append(session_id)
                run_id = uuid.UUID((body if trial == "second" else body["run"])["run_id"])
                await claim(seed, run_id, owner_sessionmaker)
                assert runtime.dispatch_web_chat_run(run_id, claim_version=1, worker_id="rl0c")
                await runtime._TASKS[run_id.hex]
                assert probe.phase.name == f"{trial}-after"
                assert len(provider.requests) == 1
                if trial in {"cold", "warm"}:
                    tasks = [
                        asyncio.create_task(start())
                        for start in (
                            runtime_control_bus.start_runtime_control_listener,
                            start_web_chat_stream_forwarder,
                            worker.start_runtime_task_worker_loop,
                            start_runtime_budget_daemon,
                        )
                    ]
                    try:
                        await asyncio.sleep(max(0, TAIL_SECONDS - (time.perf_counter() - probe.phase.wall_start)))
                        probe.switch()
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
            await assert_committed_reply(owner_sessionmaker, run_id, session_id, seed)
            timing, = [entry for entry in summaries(caplog) if entry["run_id"] == str(run_id)]
            assert timing["rounds"] == 1 and timing["instrumentation_errors"] == 0
    finally:
        await close_redis()

    assert session_ids[0] != session_ids[1] == session_ids[2]
    measured = {name: phase.metrics() for name, phase in probe.phases.items()}
    for name, phase in probe.phases.items():
        assert phase.roles <= {pg_fixtures.APP_USER}
        print("A2FP1_PHASE", name, json.dumps(measured[name]), flush=True)
    exceeded = []
    for trial, budget in CEILINGS.items():
        predicted_ms = 0.0
        for segment, (max_sql, max_transactions) in budget.items():
            name = f"{trial}-{segment}"
            metric = measured[name]
            predicted_ms += metric["predicted_ms"]
            if metric["statements"] > max_sql or metric["transactions"] > max_transactions:
                exceeded.append((name, metric["statements"], metric["transactions"], max_sql, max_transactions))
        print("A2FP1_TURN", trial, f"predicted_ms={predicted_ms:.3f}", flush=True)
    assert not exceeded, exceeded
