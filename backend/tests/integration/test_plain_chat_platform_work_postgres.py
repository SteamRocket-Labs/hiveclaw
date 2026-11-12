"""Measure platform work on the real Web worker with a production-shaped memory DAG."""

import json
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from functools import wraps
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from tests.integration.test_memory_source_read_work_postgres import memory_dag, native_workspace  # noqa: F401
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


class PlatformWork:
    def __init__(self):
        self.frame = ContextVar("platform_work_frame", default=None)
        self._window = "before_first_model"
        self.sql = defaultdict(list)
        self.ms = defaultdict(float)
        self.calls = defaultdict(int)
        self.model_calls = 0
        self.memory = []
        self.started = perf_counter()

    def tick(self):
        now = perf_counter()
        phase = self.frame.get()
        if phase is not None:
            self.ms[self.window, phase] += (now - self.started) * 1000
        self.started = now

    @property
    def window(self):
        return self._window

    @window.setter
    def window(self, value):
        self.tick()
        self._window = value

    @contextmanager
    def stage(self, phase):
        self.tick()
        token = self.frame.set(phase)
        self.calls[self.window, phase] += 1
        try:
            yield
        finally:
            self.tick()
            self.frame.reset(token)

    @contextmanager
    def counting(self):
        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            if self.window == "after_model":
                return
            phase = self.frame.get() or "other"
            self.sql[self.window, phase].append(" ".join(statement.split()))

        event.listen(Engine, "before_cursor_execute", observe)
        try:
            yield
        finally:
            event.remove(Engine, "before_cursor_execute", observe)

    def wrap(self, monkeypatch, module, name, phase):
        original = getattr(module, name)

        @wraps(original)
        async def measured(*args, **kwargs):
            label = phase
            if name == "_persist_runtime_event":
                data = kwargs.get("data", {})
                label = "event:" + str(data.get("event_type") or data.get("type") or "other")
            with self.stage(label):
                result = await original(*args, **kwargs)
            if name == "build_memory_context":
                self.memory.append({"status": result.status, "components": result.degraded_components})
            if name == "_execute_tool_with_request":
                self.window = "between_rounds"
            return result

        monkeypatch.setattr(module, name, measured)

    def report(self, case):
        rows = [{"window": window, "stage": phase, "statements": len(self.sql[window, phase]),
            "ms": round(self.ms[window, phase], 2), "calls": self.calls[window, phase],
            "top_queries": Counter(self.sql[window, phase]).most_common(8)}
            for window, phase in sorted(set(self.sql) | set(self.ms)) if window != "after_model"]
        totals = {window: sum(len(sql) for (w, _), sql in self.sql.items() if w == window)
            for window in ("before_first_model", "between_rounds")}
        print("A2D_PLATFORM_WORK", json.dumps({"case": case, "rows": rows, "totals": totals,
            "memory": self.memory}), flush=True)
        return totals


@pytest.fixture
async def hello_work(memory_dag, owner_sessionmaker, app_user_sessionmaker, monkeypatch):  # noqa: F811
    import app.database as database
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.services.runtime_root_ledger import register_runtime_root_item
    from app.services.secrets_provider import get_secrets_provider
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority

    n = memory_dag
    session_id, run_id, model_id, input_id = (uuid4() for _ in range(4))
    turn_id = f"turn-{run_id.hex}"
    async with owner_sessionmaker() as db:
        db.add(LLMModel(id=model_id, tenant_id=n.tenant, provider="openai", model="fake-4.1",
            api_key_encrypted=get_secrets_provider().encrypt("synthetic-local-provider-only"),
            label="Local fake", enabled=True, max_input_tokens=256_000))
        (await db.get(Agent, n.agent)).primary_model_id = model_id
        db.add(ChatSession(id=session_id, agent_id=n.agent, tenant_id=n.tenant, user_id=n.user,
            source_channel="web", audience_kind="private"))
        db.add(RuntimeTask(id=run_id, task_type="web_chat_turn", status="running", parent_agent_id=n.agent,
            child_agent_id=n.agent, tenant_id=n.tenant, parent_session_id=str(session_id),
            child_session_id=str(session_id), root_user_id=n.user, root_session_id=str(session_id),
            root_runtime_task_id=run_id, prompt="hello", claimed_by="a2d-worker", claim_version=1, attempt_count=1,
            claim_expires_at=datetime.now(UTC) + timedelta(minutes=10), metadata_json={"turn_id": turn_id,
                "user_id": str(n.user), "runtime_budget": {"schema": "hive.runtime_budget_binding.v1",
                    "status": "active", "interactive": True}}))
        await db.flush()
        await register_runtime_root_item(db, tenant_id=n.tenant, root_runtime_task_id=run_id,
            source_agent_id=n.agent, intent_key=f"web-chat-turn:{run_id.hex}", work_type="web_chat_turn",
            target_ref=str(session_id), runtime_task_id=run_id, root_user_id=n.user,
            root_session_id=str(session_id), state="running")
        await db.commit()
        authority = await resolve_session_mutation_authority(db, user=await db.get(User, n.user),
            agent_id=n.agent, session_id=session_id, action="mutate_session_input")
        await accept_human_input(db, authority=authority, intent={"kind": "steer_current_turn",
            "input_id": str(input_id), "idempotency_key": f"input:{input_id}", "session_id": str(session_id),
            "content_parts": [{"type": "text", "text": "hello"}], "expected_turn_id": turn_id,
            "expected_run_id": str(run_id), "terminal_fallback": "queue_next_turn"})
        await run_user_prompt_admission(db, authority=authority, input_id=input_id, worker_id="a2d-worker")
        await queue_admitted_human_input(db, authority=authority, input_id=input_id)
        await db.commit()
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    n.web_session, n.web_run = session_id, run_id
    return n


@pytest.mark.parametrize("case", ["hello", "one_tool", "unverifiable_source"])
async def test_plain_chat_platform_work(hello_work, app_user_sessionmaker, monkeypatch, case):
    from app.database import tenant_scoped_session
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.kernel import engine
    from app.runtime import hooks, invocation_orchestrator, invoker
    from app.services import agent_directory_sources, web_chat_run_orchestrator, web_chat_runtime
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence

    n, probe = hello_work, PlatformWork()
    tool_round = case == "one_tool"
    if case == "unverifiable_source":
        (n.root / "memory/self/self.md").write_text("Changed after the original owner witness.\n")
    from app.services import explicit_memory_authority

    authorize = explicit_memory_authority.authorized_explicit_entries
    issues = []

    @wraps(authorize)
    async def observe_issues(**kwargs):
        result = await authorize(**kwargs)
        issues.extend(kwargs.get("issue_observer") or [])
        return result

    monkeypatch.setattr(explicit_memory_authority, "authorized_explicit_entries", observe_issues)
    script = [{"content": "Hello!", "stream_text": "Hello!", "finish_reason": "stop"}]
    if tool_round:
        script.insert(0, {"tool_calls": [{"id": "read-soul", "type": "function", "function": {
            "name": "read_context_resource", "arguments": json.dumps({"ref": "soul"})}}],
            "finish_reason": "tool_calls"})

    class MeasuredClient(ScriptedLLMClient):
        async def stream(self, **kwargs):
            probe.model_calls += 1
            probe.window = "after_model"
            return await super().stream(**kwargs)

    client = MeasuredClient(script)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    for module, name, phase in [
        (web_chat_runtime, "_load_runtime_context", "load_run_context"),
        (web_chat_runtime, "_resolve_runtime_models_for_task", "model_route"),
        (web_chat_runtime, "_persist_runtime_event", "event"),
        (invocation_orchestrator, "_run_setup_hook", "hooks"),
        (invocation_orchestrator, "_run_prompt_hook", "hooks"),
        (invocation_orchestrator, "_run_session_start_hook", "hooks"),
        (hooks, "emit_hook", "hooks"),
        (invoker, "build_memory_context", "memory_context"),
        (invoker, "_resolve_memory_context", "memory_status"),
        (invoker, "_build_system_prompt", "system_prompt"),
        (invoker, "_resolve_runtime_metadata_context", "runtime_context"),
        (invoker, "_build_native_skill_catalog", "skill_catalog"),
        (invoker, "get_agent_tools_for_llm", "tool_configuration"),
        (invoker, "_resolve_runtime_config", "runtime_configuration"),
        (invoker, "_enforce_invocation_quota", "quota"),
        (invoker, "_load_invocation_secret_boundary", "credential_boundary"),
        (invoker, "_execute_tool_with_request", "tool_execution"),
        (agent_directory_sources, "observe_directory", "agent_directory"),
        (engine, "prepare_session_context_for_request", "context_window_compaction"),
        (web_chat_run_orchestrator, "_prepare_session_model_request", "result_commit_prepare"),
        (web_chat_run_orchestrator, "_bind_session_round_inputs", "bind_round_inputs"),
    ]:
        probe.wrap(monkeypatch, module, name, phase)
    fence = set_runtime_task_fence(task_id=n.web_run, worker_id="a2d-worker", claim_version=1)
    try:
        with probe.counting(), probe.stage("other"):
            await web_chat_runtime.execute_web_chat_run(n.web_run)
    finally:
        reset_runtime_task_fence(fence)
        totals = probe.report(case)
        print("A2D_MEMORY_ISSUES", json.dumps({"case": case, "issues": issues}), flush=True)
    assert len(client.requests) == 1 + tool_round
    assert "hello" in json.dumps(client.requests[0]["messages"])
    if case == "unverifiable_source":
        assert all(f"MEMORY_DAG_{index}" not in json.dumps(request["messages"])
            for request in client.requests for index in range(4, 10))
        assert "explicit_memory_source_authority" in probe.memory[0]["components"]
        assert issues and all(issue["code"] == "content_source_changed" for issue in issues), issues
    else:
        assert all("MEMORY_DAG_9" in json.dumps(request["messages"]) for request in client.requests)
        assert probe.memory == [{"status": "ready", "components": ()}]
    if tool_round:
        assert "Help the owner" in json.dumps(client.requests[1]["messages"])
        assert totals["between_rounds"] > 0
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        run = await db.get(RuntimeTask, n.web_run)
        assert run.status == "completed", run.result_summary
        results = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == n.web_run)))
        assert len(results) == 1 + tool_round and all(row.state == "round_committed" for row in results)
        tools = list(await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == n.web_run)))
        assert len(tools) == tool_round
        assert all(tool.effect_state == "effect_committed" and tool.result_event_id is not None for tool in tools)
    if case == "hello":
        # The baseline is 818 statements; acceptance requires at least halving it.
        assert totals["before_first_model"] <= 409, totals
    elif case == "unverifiable_source":
        assert totals["before_first_model"] <= 321, totals
    elif tool_round:
        assert totals["between_rounds"] <= 161, totals


async def test_later_memory_source_refreshes_platform_skill_lineage(
    hello_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.database import tenant_scoped_session
    from app.models.organization_identity import ContentInputReceipt
    from app.services import content_input_receipts
    from app.services.config_versioning import save_revision
    from app.services.explicit_memory_authority import source_dependency
    from app.services.session_model_round import _validate_or_withhold_model_sources
    from app.services.skill_source_receipts import _installation_id
    from tests.integration.test_a4c_one_item_withheld import _bindings

    n = hello_work
    async with owner_sessionmaker() as writer:
        for index in range(2):
            receipt = await writer.get(ContentInputReceipt, n.receipts[index + 4].id)
            receipt.dependencies_json = [n.shared[index]]
        await writer.commit()
    sources = [source_dependency(receipt) for receipt in n.receipts[4:6]]
    validate, changed = content_input_receipts._validate_input_dependency_batch, False

    async def install_after_first_source(db, tenant_id, dependencies, **kwargs):
        nonlocal changed
        await validate(db, tenant_id, dependencies, **kwargs)
        if dependencies == [sources[0]] and not changed:
            folder = n.shared[1]["source_id"].split("\n")[1].split("/")[1]
            async with owner_sessionmaker() as writer:
                await save_revision(writer, tenant_id=n.tenant, entity_type="agent_skill_install",
                    entity_id=_installation_id(n.agent, folder), content={"schema": "hive.agent_skill_install.v1"},
                    change_source="user", changed_by_user_id=n.user)
                await writer.commit()
            changed = True

    monkeypatch.setattr(content_input_receipts, "_validate_input_dependency_batch", install_after_first_source)
    messages = [{"role": "system", "content": content} for content in ("FIRST_MEMORY", "SECOND_MEMORY")]
    bindings = _bindings(2, {index: [source] for index, source in enumerate(sources)})
    wire = {"messages": [dict(message) for message in messages]}
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        await _validate_or_withhold_model_sources(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.web_session, run_id=n.web_run, input_bindings=bindings, messages=messages, wire_request=wire)
        await db.rollback()
    assert changed
    assert bindings["messages"][0]["status"] == "bound"
    assert bindings["messages"][1]["status"] == "withheld"
    assert bindings["messages"][1]["code"] == "content_source_unavailable"
    assert bindings["dependencies"] == [sources[0]]
    assert wire["messages"][0]["content"] == "FIRST_MEMORY"
    assert "SECOND_MEMORY" not in json.dumps(wire)


async def test_recovered_memory_source_restores_the_ready_index(
    memory_dag, owner_sessionmaker,  # noqa: F811
):
    from app.models.organization_identity import ContentInputReceipt
    from tests.integration.test_memory_source_read_work_postgres import resolve

    n = memory_dag
    original = list(n.receipts[9].dependencies_json)
    async with owner_sessionmaker() as writer:
        row = await writer.get(ContentInputReceipt, n.receipts[9].id)
        row.dependencies_json = [*original, {**n.shared[0], "source_version": "unavailable-version"}]
        await writer.commit()

    withheld = await resolve(n)
    assert "MEMORY_DAG_9" not in withheld.content, withheld.content
    assert "explicit_memory_source_authority" in withheld.degraded_components, withheld

    async with owner_sessionmaker() as writer:
        (await writer.get(ContentInputReceipt, n.receipts[9].id)).dependencies_json = original
        await writer.commit()
    result = await resolve(n)
    assert result.status == "ready", result
    assert "explicit_memory_source_authority" not in result.degraded_components
    assert "MEMORY_DAG_9" in result.content


async def test_authority_failure_withholds_the_whole_explicit_layer(
    memory_dag, monkeypatch,  # noqa: F811
):
    from app.services import explicit_memory_authority
    from tests.integration.test_memory_source_read_work_postgres import resolve

    n = memory_dag
    authorize = explicit_memory_authority.authorized_explicit_entries

    async def failing_authorize(**kwargs):
        raise OSError("synthetic explicit-authority outage")

    monkeypatch.setattr(explicit_memory_authority, "authorized_explicit_entries", failing_authorize)
    result = await resolve(n)

    assert result.status == "degraded"
    assert result.degraded_components == ("explicit_memory_source_authority",)
    assert all(f"MEMORY_DAG_{index}" not in result.content for index in range(4, 10))
    assert all(source["source_kind"] != "explicit_memory" for source in result.source_refs)
    # A4c in the prompt: an outage the count cannot survive says 部分, never silence.
    assert result.content.rsplit("\n\n", 1)[-1] == "有部分记忆暂时无法核实，已略过，可继续使用其余内容。"
    assert "有1 条记忆暂时无法核实" not in result.content
    del authorize


async def test_bulk_tool_inventory_keeps_config_precedence(
    native_workspace, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    from app.database import tenant_scoped_session
    from app.models.tenant_tool_config import TenantToolConfig
    from app.models.tool import AgentTool, Tool
    from app.services.tool_config_service import resolve_tool_config, resolve_tool_configs

    n = native_workspace
    primary, tenant_only, missing = (f"a2d-{uuid4().hex}" for _ in range(3))
    schema = {"fields": [{"key": "api_key", "type": "password"}, {"key": "timeout", "type": "number"}]}
    async with owner_sessionmaker() as writer:
        base = Tool(name=primary, display_name="Global fixture", config={"api_key": "synthetic-global", "timeout": 1},
            config_schema=schema)
        writer.add_all([base,
            Tool(name=primary, display_name="Tenant shadow", tenant_id=n.tenant, config={"api_key": "synthetic-shadow"}),
            Tool(name=tenant_only, display_name="Tenant fixture", tenant_id=n.tenant, config={"timeout": 3})])
        await writer.flush()
        writer.add(TenantToolConfig(tenant_id=n.tenant, tool_id=base.id,
            config={"api_key": "synthetic-company", "timeout": 2}))
        writer.add(AgentTool(tenant_id=n.tenant, agent_id=n.agent, tool_id=base.id,
            config={"api_key": "synthetic-personal"}))
        await writer.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        bulk = await resolve_tool_configs([primary, tenant_only, missing], n.tenant, agent_id=n.agent, db=db)
        singles = {name: await resolve_tool_config(name, n.tenant, agent_id=n.agent, db=db)
            for name in (primary, tenant_only, missing)}
    assert bulk == singles
    assert bulk[primary] == {"api_key": "synthetic-personal", "timeout": 2}
    assert bulk[tenant_only] == {"timeout": 3}
    assert bulk[missing] == {}


async def test_combined_reader_query_keeps_the_selected_company_with_owner_connection(
    native_workspace, owner_sessionmaker,  # noqa: F811
):
    from app.core.permissions import load_runtime_user_agent_access
    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id, user_id, agent_id = (uuid4() for _ in range(3))
    async with owner_sessionmaker() as writer:
        writer.add(Tenant(id=tenant_id, name="Foreign fixture", slug=f"foreign-{tenant_id.hex}"))
        await writer.flush()
        writer.add(User(id=user_id, username=f"foreign-{user_id.hex}", email=f"{user_id.hex}@fixture.test",
            display_name="Foreign reader", tenant_id=tenant_id, password_hash="synthetic", role="member"))
        await writer.flush()
        writer.add(Agent(id=agent_id, tenant_id=tenant_id, creator_id=user_id, owner_user_id=user_id,
            name="Foreign employee"))
        await writer.commit()
    async with tenant_scoped_session(native_workspace.tenant, session_factory=owner_sessionmaker) as db:
        reader, agent = await load_runtime_user_agent_access(db, tenant_id=native_workspace.tenant,
            user_id=user_id, agent_id=agent_id)
    assert reader is None and agent is None
