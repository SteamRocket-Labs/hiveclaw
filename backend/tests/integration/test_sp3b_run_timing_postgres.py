"""Per-run timing on the native member/worker path, without provider network."""

import asyncio
import hashlib
import inspect
import json
import logging
import time
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from app.core import request_timing as timing_module
from app.models.runtime_task import RuntimeTask
from tests.integration.test_a2p_member_request_cost_postgres import member_request  # noqa: F401, F811
from tests.integration.test_a2p_server_timing_postgres import timing_engine  # noqa: F401, F811
from tests.integration.test_sp1_send_statement_budget_postgres import claim, configure, execute
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@contextmanager
def observed_sql():
    sample = {"statements": 0, "transactions": 0, "hashes": set()}

    def before(_conn, _cursor, statement, _parameters, _context, _many):
        sample["statements"] += 1
        sql_hash = hashlib.md5(statement.encode("utf-8")[:1023].decode("utf-8", errors="ignore").encode("utf-8")).hexdigest()[:12]
        sample["hashes"].add(sql_hash)

    def begin(*_args):
        sample["transactions"] += 1

    event.listen(Engine, "before_cursor_execute", before)
    event.listen(Engine, "begin", begin)
    try:
        yield sample
    finally:
        event.remove(Engine, "before_cursor_execute", before)
        event.remove(Engine, "begin", begin)


def summaries(caplog):
    return [json.loads(record.getMessage().removeprefix("run_timing "))
        for record in caplog.records if record.getMessage().startswith("run_timing ")]


@pytest.fixture
async def scenario(member_request, owner_sessionmaker, monkeypatch, tmp_path):  # noqa: F811
    from app.services import agent_tools

    seed, _client, _sample, factory = member_request
    seed, client = await configure(member_request, owner_sessionmaker, factory, monkeypatch, tmp_path)

    async def start():
        response = await client.post(f"/api/agents/{seed.agent_id}/sessions/runs", json={
            "content": "SP3b private message sentinel", "input_id": str(uuid.uuid4())})
        assert response.status_code == 201, response.text
        run = uuid.UUID(response.json()["run"]["run_id"])
        await claim(seed, run, owner_sessionmaker)
        # This scenario exercises collection itself, even when earlier tests
        # warmed the process-wide collection and core-schema caches.
        monkeypatch.setattr(agent_tools, "_COLLECTED_TOOLS", None)
        monkeypatch.setattr(agent_tools, "_always_core_tools", None)
        return run

    return seed, factory, start


async def test_scripted_run_has_one_content_free_partitioned_summary(
    scenario, owner_sessionmaker, monkeypatch, caplog,
):
    import app.runtime.invoker as invoker
    import app.kernel.engine as engine
    from app.services import agent_tools

    seed, factory, start = scenario
    # Earlier tests can leave the process-wide registry warm. Exercise that
    # state before the fixture establishes the cold collection scenario.
    assert agent_tools._get_collected_tools().openai_tools
    agent_tools._get_always_core_tools()
    agent_tools._get_feishu_tools()
    agent_tools._get_hr_tools()
    from app.tools import governance, registry
    for names in (registry.READ_ONLY_TOOL_NAMES, registry.PARALLEL_SAFE_TOOL_NAMES,
        governance.SAFE_TOOLS, governance.SENSITIVE_TOOLS):
        tuple(names)
    registry.result_char_limit_for_tool("get_current_time")
    from app.services.governance_capability_taxonomy import taxonomy_policy_capability_group_names_for_tool
    taxonomy_policy_capability_group_names_for_tool("send_feishu_message")
    run = await start()

    class Provider(ScriptedLLMClient):
        async def stream(self, **kwargs):
            provider_moments.append(time.perf_counter())
            assert timing_module.request_timing.get().first_provider_at is not None
            await asyncio.sleep(0.02)
            return await super().stream(**kwargs)

    provider_moments = []
    round_moments = []
    original_stream = engine._stream_with_cancel

    async def observe_round(*args, **kwargs):
        round_moments.append(time.perf_counter())
        return await original_stream(*args, **kwargs)

    monkeypatch.setattr(engine, "_stream_with_cancel", observe_round)
    provider = Provider([
        {"tool_calls": [{"id": "sp3b-clock", "type": "function", "function": {
            "name": "get_current_time", "arguments": '{"timezone":"UTC"}'}}]},
        {"content": "SP3b private answer sentinel", "stream_text": "SP3b private answer sentinel"},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
    scopes = []
    original_scope = timing_module.run_timing_scope

    @contextmanager
    def capture_scope(*args, **kwargs):
        with original_scope(*args, **kwargs) as timing:
            scopes.append(timing)
            yield timing

    monkeypatch.setattr(timing_module, "run_timing_scope", capture_scope)
    caplog.set_level(logging.INFO, logger="app.core.request_timing")
    with observed_sql() as observed:
        started = time.perf_counter()
        await execute(run)
        wall_ms = (time.perf_counter() - started) * 1000
    async with owner_sessionmaker() as db:
        assert (await db.get(RuntimeTask, run)).status == "completed"
    lines = summaries(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert line["run_id"] == str(run) and line["task_type"] == "web_chat_turn"
    assert line["rounds"] == len(provider.requests) == 2
    assert len(provider_moments) == 2
    assert len(round_moments) == 2
    assert round_moments[0] <= scopes[0].first_provider_at <= provider_moments[0]
    assert line["pre_model_ms"] == pytest.approx(
        (round_moments[0] - scopes[0].started) * 1000, abs=1)
    assert line["db_statements"] == observed["statements"]
    assert line["transactions"] == observed["transactions"]
    partition = sum(line[name] for name in (
        "pre_model_ms", "model_ms", "between_rounds_ms", "post_model_ms"))
    assert abs(partition - wall_ms) <= wall_ms * 0.05
    assert line["model_ms"] >= 35 and line["between_rounds_ms"] > 0
    assert line["db_ms"] > 0 and line["pool_ms"] >= 0
    assert "rows" not in line
    assert 0 < len(line["statements"]) <= 10
    assert 0 < len(line["gaps"]) <= 10
    for entries in (line["statements"], line["gaps"], line["phases"]):
        assert [entry["ms"] for entry in entries] == sorted(
            (entry["ms"] for entry in entries), reverse=True)
    assert {span["name"] for span in line["phases"]} == {
        "tool_collection", "prompt_build", "memory_context", "model_input_source_validation", "hook_dispatch",
        "skill_catalog", "secret_boundary"}
    numeric = {"wall_ms", "cleanup_ms", "pre_model_ms", "model_ms", "between_rounds_ms", "post_model_ms",
        "rounds", "db_statements", "db_ms", "pool_ms", "transactions", "instrumentation_errors"}
    assert set(line) == numeric | {"run_id", "task_type", "statements", "gaps", "phases"}
    assert all(isinstance(line[key], (int, float)) for key in numeric)
    for entry in line["statements"]:
        assert set(entry) == {"hash", "frame", "ms"}
        assert entry["hash"] in observed["hashes"]
    for entry in line["gaps"]:
        assert set(entry) == {"from", "to", "ms"}
        assert entry["from"].startswith("app.") and entry["to"].startswith("app.")
    assert all(set(entry) == {"name", "ms", "n"} and entry["n"] >= 1 for entry in line["phases"])
    # Independently verified source identifiers are authorized log fields;
    # names such as get_agent_timezone are not copies of tool arguments.
    content_fields = {**line,
        "statements": [{**entry, "frame": ""} for entry in line["statements"]],
        "gaps": [{**entry, "from": "", "to": ""} for entry in line["gaps"]]}
    serialized = json.dumps(content_fields)
    for forbidden in ("SELECT", "INSERT", "current_tenant", "private message", "private answer", "timezone"):
        assert forbidden not in serialized
    assert all(entry["frame"].startswith("app.") for entry in line["statements"])
    print("SP3B_RUN", json.dumps(line), "WALL_MS", round(wall_ms, 3), flush=True)


async def test_instrumentation_failure_does_not_change_run_completion(
    scenario, owner_sessionmaker, monkeypatch, caplog,
):
    import app.runtime.invoker as invoker

    _seed, _factory, start = scenario
    run = await start()
    provider = ScriptedLLMClient([{"content": "The local result is ready."}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)

    def broken(*_args, **_kwargs):
        raise RuntimeError("instrumentation private error sentinel")

    monkeypatch.setattr(timing_module, "_application_frame", broken, raising=False)
    caplog.set_level(logging.INFO, logger="app.core.request_timing")
    await execute(run)
    async with owner_sessionmaker() as db:
        assert (await db.get(RuntimeTask, run)).status == "completed"
    lines = summaries(caplog)
    assert len(lines) == 1 and lines[0]["instrumentation_errors"] > 0
    assert "private error" not in json.dumps(lines)
    assert timing_module.request_timing.get() is None


async def test_driver_hash_transaction_gaps_and_bounded_top_ten(app_user_engine, caplog):
    sql = "SELECT 42 AS sp3b_known, '" + "a" * 993 + "界" * 20 + "' AS label"
    expected = hashlib.md5(sql.encode("utf-8")[:1023].decode("utf-8", errors="ignore").encode("utf-8")).hexdigest()[:12]
    caplog.set_level(logging.INFO, logger="app.core.request_timing")
    with timing_module.run_timing_scope(uuid.uuid4()):
        with observed_sql() as observed:
            async with app_user_engine.connect() as conn:
                for _ in range(12):
                    await conn.execute(text(sql))
                await asyncio.sleep(0.025)
                await conn.execute(text(sql))
                await conn.commit()
                # A larger idle interval outside a transaction must be excluded.
                await asyncio.sleep(0.07)
                await conn.execute(text(sql))
                await conn.rollback()
    line, = summaries(caplog)
    assert line["db_statements"] == observed["statements"] == 14
    assert line["transactions"] == observed["transactions"] == 2
    assert len(line["statements"]) == len(line["gaps"]) == 10
    assert {entry["hash"] for entry in line["statements"]} == {expected}
    assert 20 <= line["gaps"][0]["ms"] < 65
    assert all(entry["from"] and entry["to"] for entry in line["gaps"])
    assert line["rounds"] == 0 and line["model_ms"] == line["post_model_ms"] == 0
    assert sql not in json.dumps(line)

    caplog.clear()
    with timing_module.run_timing_scope(uuid.uuid4()):
        async with app_user_engine.connect() as conn:
            async with conn.stream(text("SELECT 42")) as result:
                assert await result.all() == [(42,)]
    streamed, = summaries(caplog)
    assert "rows" not in streamed


@pytest.mark.parametrize("end", ["commit", "rollback"])
async def test_transaction_end_records_last_gap_and_known_application_frames(
    app_user_sessionmaker, caplog, end,
):
    from app.database import tenant_scoped_session

    class Rollback(Exception):
        pass

    caplog.set_level(logging.INFO, logger="app.core.request_timing")
    with timing_module.run_timing_scope(uuid.uuid4()):
        try:
            async with tenant_scoped_session(uuid.uuid4(), session_factory=app_user_sessionmaker):
                # The RLS statement is issued by this known application function.
                await asyncio.sleep(0.06)
                if end == "rollback":
                    raise Rollback()
        except Rollback:
            pass
    line, = summaries(caplog)
    assert line["db_statements"] == line["transactions"] == 1
    statement, = line["statements"]
    gap, = line["gaps"]
    assert statement["frame"].rsplit(":", 1)[0] == "app.database:_apply_rls_tenant_for_transaction"
    assert gap["from"] == statement["frame"]
    source, first_line = inspect.getsourcelines(tenant_scoped_session.__wrapped__)
    end_line, = [first_line + index for index, value in enumerate(source)
        if value.strip() == f"await session.{end}()"]
    assert gap["to"] == f"app.database:tenant_scoped_session:{end_line}"
    assert gap["ms"] >= 50


async def test_pool_wait_and_repeated_phase_count(timing_engine, caplog):  # noqa: F811
    entered = asyncio.Event()
    held = await timing_engine.connect()

    @timing_module.timed_phase("tool_collection")
    async def phase():
        await asyncio.sleep(0.001)

    async def release():
        await entered.wait()
        await asyncio.sleep(0.06)
        await held.close()

    caplog.set_level(logging.INFO, logger="app.core.request_timing")
    releaser = asyncio.create_task(release())
    try:
        with timing_module.run_timing_scope(uuid.uuid4()):
            entered.set()
            async with timing_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            await phase()
            await phase()
    finally:
        await held.close()
        await releaser
    line, = summaries(caplog)
    assert line["pool_ms"] >= 50
    span, = line["phases"]
    assert span["name"] == "tool_collection" and span["n"] == 2 and span["ms"] >= 2


@pytest.mark.parametrize("provider,stream", [
    ("openai", False), ("openai", True), ("anthropic", True), ("gemini", True),
])
async def test_native_provider_request_boundaries(provider, stream, monkeypatch, caplog):
    import httpx
    from app.kernel.engine import _stream_with_cancel
    from app.services.llm_client import (
        AnthropicClient, GeminiClient, LLMMessage, OpenAICompatibleClient, create_llm_client,
    )

    async def respond(request):
        await asyncio.sleep(0.025)
        if not stream:
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        if provider == "anthropic":
            payload = ('event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                '"delta":{"type":"text_delta","text":"ok"}}\n\n'
                'event: message_stop\ndata: {"type":"message_stop"}\n\n')
        elif provider == "gemini":
            payload = 'data: {"candidates":[{"content":{"parts":[{"text":"ok"}]},"finishReason":"STOP"}]}\n\n'
        else:
            payload = 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield payload.encode()

        return httpx.Response(200, stream=Body(), headers={"content-type": "text/event-stream"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    client = create_llm_client(provider=provider, api_key="synthetic-local-only", model="local-reference")
    leaf = getattr(client, "_inner", client)
    assert isinstance(leaf, {"openai": OpenAICompatibleClient, "anthropic": AnthropicClient, "gemini": GeminiClient}[provider])
    monkeypatch.setattr(leaf, "_client", http)
    caplog.set_level(logging.INFO, logger="app.core.request_timing")
    try:
        with timing_module.run_timing_scope(uuid.uuid4()) as timing:
            await asyncio.sleep(0.015)
            call = client.stream if stream else client.complete
            args = {"messages": [LLMMessage(role="user", content="private transport sentinel")], "max_tokens": 128}
            auxiliary = await call(**args)
            assert auxiliary.content == "ok"
            assert timing.first_provider_at is None and timing.rounds == 0 and timing.model_seconds == 0
            child = asyncio.create_task(call(**args))
            started = time.perf_counter()
            response = await _stream_with_cancel(SimpleNamespace(stream=call), cancel_event=None, **args)
            finished = time.perf_counter()
            assert response.content == "ok"
            assert started <= timing.first_provider_at <= finished
            assert timing.model_seconds == pytest.approx(finished - started, abs=0.005)
            first, model_seconds = timing.first_provider_at, timing.model_seconds
            assert (await child).content == "ok"
            # Auxiliary calls before terminal commit, including an inherited
            # child task overlapping the main round, never create rounds.
            assert (await call(**args)).content == "ok"
            assert timing.first_provider_at == first and timing.model_seconds == model_seconds
            assert timing.rounds == 1
            timing_module.mark_run_terminal()
            # Post-turn maintenance can use the same client after the run's
            # terminal commit; it must not become another run round.
            maintenance = await call(messages=[LLMMessage(role="user", content="private maintenance sentinel")], max_tokens=128)
            assert maintenance.content == "ok"
    finally:
        await client.close()
        await http.aclose()
    line, = summaries(caplog)
    assert line["rounds"] == 1
    assert line["pre_model_ms"] >= 35 and line["model_ms"] >= 20
    assert line["post_model_ms"] >= 0 and line["cleanup_ms"] >= 20
    assert "private transport" not in json.dumps(line)


@pytest.mark.parametrize("stream", [False, True])
async def test_http_retry_is_one_model_round_including_backoff(stream, monkeypatch, caplog):
    import httpx
    from app.kernel.engine import _stream_with_cancel
    from app.services.llm_client import LLMMessage, create_llm_client

    attempts = []

    async def respond(_request):
        attempts.append(time.perf_counter())
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "0.06"}, json={"error": "local retry"})
        if not stream:
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

        return httpx.Response(200, stream=Body())

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    client = create_llm_client(provider="openai", api_key="local-only", model="local-reference")
    monkeypatch.setattr(getattr(client, "_inner", client), "_client", http)
    caplog.set_level(logging.INFO, logger="app.core.request_timing")
    try:
        with timing_module.run_timing_scope(uuid.uuid4()) as timing:
            started = time.perf_counter()
            response = await _stream_with_cancel(
                SimpleNamespace(stream=client.stream if stream else client.complete), cancel_event=None,
                messages=[LLMMessage(role="user", content="hello")], _http_max_attempts=2)
            finished = time.perf_counter()
            assert started <= timing.first_provider_at <= attempts[0]
            assert timing.model_seconds == pytest.approx(finished - started, abs=0.005)
    finally:
        await client.close()
        await http.aclose()
    line, = summaries(caplog)
    assert response.content == "ok" and len(attempts) == 2
    assert line["rounds"] == 1 and line["model_ms"] >= 50
    assert line["between_rounds_ms"] == 0


async def test_provider_observer_failure_is_non_fatal(monkeypatch, caplog):
    import httpx
    from app.kernel.engine import _stream_with_cancel
    from app.services.llm_client import create_llm_client, LLMMessage

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

    async def respond(_request):
        return httpx.Response(200, stream=Body())

    def broken(*_args):
        raise RuntimeError("private observer initialization sentinel")

    monkeypatch.setattr(timing_module, "_provider_finished", broken, raising=False)
    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    client = create_llm_client(provider="openai", api_key="local-only", model="local-reference")
    monkeypatch.setattr(getattr(client, "_inner", client), "_client", http)
    caplog.set_level(logging.INFO, logger="app.core.request_timing")
    try:
        with timing_module.run_timing_scope(uuid.uuid4()):
            response = await _stream_with_cancel(client, cancel_event=None,
                messages=[LLMMessage(role="user", content="hello")], _http_max_attempts=1)
    finally:
        await client.close()
        await http.aclose()
    assert response.content == "ok"
    line, = summaries(caplog)
    assert line["instrumentation_errors"] == 1
    assert line["rounds"] == 1
    assert "private observer" not in json.dumps(line)
