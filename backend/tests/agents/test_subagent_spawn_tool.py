"""Tests for the public spawn_subagent entry + its tool handler (cut ②).

Style mirrors test_subagent.py: inject a fake invoke / monkeypatch the DB-bound
resolver so no real DB or LLM is touched.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.subagent import (
    SubagentHandle,
    SubagentResult,
    SubagentSpawnContext,
    SubagentSpec,
    explorer_spec,
    spawn_subagent,
)
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest


def _spawn_ctx(**overrides) -> SubagentSpawnContext:
    ctx = SubagentSpawnContext(
        parent_agent_id=uuid.uuid4(),
        parent_user_id=uuid.uuid4(),
        model=SimpleNamespace(provider="anthropic", model="claude-x"),
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def _ok_invoke(content: str = "done", tokens: int = 5):
    async def invoke(_request):
        return SimpleNamespace(content=content, tokens_used=tokens)

    return invoke


def _tool_request(
    arguments: dict,
    *,
    session_id: str | None = "sess-1",
    tenant_id: str | None = None,
) -> ToolExecutionRequest:
    context = ToolExecutionContext(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tenant_id=tenant_id,
        workspace=Path("/tmp"),
        session_id=session_id,
    )
    return ToolExecutionRequest(tool_name="spawn_subagent", arguments=arguments, context=context)


# --- public spawn_subagent entry --------------------------------------------


@pytest.mark.asyncio
async def test_spawn_subagent_returns_resolved_handle():
    ctx = _spawn_ctx(trace_id="tr-9", depth=1)
    handle = await spawn_subagent(ctx, explorer_spec("scout"), "task", invoke=_ok_invoke(content="done"))
    assert handle.name == "scout"
    assert handle.trace_id == "tr-9"
    assert handle.depth == 2  # ctx.depth + 1
    assert handle.result is not None
    assert handle.result.ok
    assert handle.result.content == "done"


@pytest.mark.asyncio
async def test_spawn_subagent_depth_limit_yields_failed_handle():
    ctx = _spawn_ctx(depth=2, max_depth=2)  # child depth 3 > 2
    handle = await spawn_subagent(ctx, explorer_spec("scout"), "task", invoke=_ok_invoke())
    assert handle.result is not None
    assert handle.result.status == "depth_limited"


# --- spawn_subagent tool handler --------------------------------------------


@pytest.mark.asyncio
async def test_spawn_tool_requires_task():
    import app.tools.handlers.subagent as handler_mod

    out = await handler_mod.spawn_subagent_tool(_tool_request({}))
    data = json.loads(out)
    assert data["ok"] is False
    assert "task" in data["error"]


@pytest.mark.asyncio
async def test_spawn_tool_reports_missing_model(monkeypatch):
    import app.tools.handlers.subagent as handler_mod

    async def fake_resolve(agent_id):
        return None, None, None

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    out = await handler_mod.spawn_subagent_tool(_tool_request({"task": "t"}))
    data = json.loads(out)
    assert data["ok"] is False
    assert "No model" in data["error"]


@pytest.mark.asyncio
async def test_spawn_tool_resolves_model_and_spawns(monkeypatch):
    import app.tools.handlers.subagent as handler_mod

    captured: dict = {}

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["ctx"] = ctx
        captured["spec"] = spec
        captured["task"] = task
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type="explorer", status="completed", content="digest", tokens_used=7),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    out = await handler_mod.spawn_subagent_tool(
        _tool_request({"task": "investigate", "name": "scout", "max_tool_rounds": 5})
    )
    data = json.loads(out)
    assert data["ok"] is True
    assert data["status"] == "completed"
    assert data["content"] == "digest"
    assert data["tokens_used"] == 7
    assert captured["task"] == "investigate"
    assert captured["spec"].name == "scout"
    assert captured["spec"].max_tool_rounds == 5
    assert captured["ctx"].parent_session_id == "sess-1"
    assert captured["ctx"].parent_agent_name == "HR"


@pytest.mark.asyncio
async def test_spawn_tool_wires_llm_memory_distiller(monkeypatch):
    """The self-evolution loop must be LIVE in production: when a memory store
    exists, the spawn context carries an LLM How-distiller bound to the
    parent's model — without it, 记忆.md is read-only forever."""

    import app.tools.handlers.subagent as handler_mod

    captured: dict = {}

    async def fake_resolve(agent_id):
        model = SimpleNamespace(provider="openai", api_key="k", model="m", base_url=None)
        return model, None, SimpleNamespace(name="HR")

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["ctx"] = ctx
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="ok"),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    out = await handler_mod.spawn_subagent_tool(_tool_request({"task": "t"}, tenant_id=str(uuid.uuid4())))
    assert json.loads(out)["ok"] is True
    assert captured["ctx"].memory_store is not None
    assert captured["ctx"].memory_distiller is not None


@pytest.mark.asyncio
async def test_spawn_tool_inline_type_selects_builtin(monkeypatch):
    """CC parity: all three builtin types are reachable from the spawn surface."""

    import app.tools.handlers.subagent as handler_mod

    captured: dict = {}

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["spec"] = spec
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="ok"),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    out = await handler_mod.spawn_subagent_tool(_tool_request({"task": "verify this", "type": "critic"}))
    data = json.loads(out)
    assert data["ok"] is True
    assert data["type"] == "critic"
    assert captured["spec"].type == "critic"
    assert captured["spec"].name == "critic"  # name defaults to the type


@pytest.mark.asyncio
async def test_spawn_tool_rejects_unknown_inline_type(monkeypatch):
    import app.tools.handlers.subagent as handler_mod

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)

    out = await handler_mod.spawn_subagent_tool(_tool_request({"task": "t", "type": "bogus"}))
    data = json.loads(out)
    assert data["ok"] is False
    assert "bogus" in data["error"]
    assert "explorer" in data["error"]  # lists the valid builtin types


@pytest.mark.asyncio
async def test_spawn_tool_can_load_persistent_definition(monkeypatch, tmp_path):
    import app.tools.handlers.subagent as handler_mod
    from app.agents.subagent_definition import definition_store_for_tenant
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    tenant_id = str(uuid.uuid4())
    definition_store_for_tenant(tenant_id, agent_data_dir=tmp_path).save(
        SubagentSpec(
            name="critic-def",
            description="Persistent critic for diff review.",
            type="critic",
            allowed_tools=("read_file",),
            max_tool_rounds=3,
            system_prompt="Persistent critic prompt.",
        )
    )
    captured: dict = {}

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["ctx"] = ctx
        captured["spec"] = spec
        captured["task"] = task
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="digest", tokens_used=7),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    out = await handler_mod.spawn_subagent_tool(
        _tool_request({"task": "review", "definition_name": "critic-def"}, tenant_id=tenant_id)
    )
    data = json.loads(out)

    assert data["ok"] is True
    assert data["definition_scope"] == "tenant"
    assert captured["task"] == "review"
    assert captured["spec"].name == "critic-def"
    assert captured["spec"].type == "critic"
    assert captured["spec"].allowed_tools == ("read_file",)
    assert captured["spec"].system_prompt == "Persistent critic prompt."
    assert captured["ctx"].memory_store is not None


# --- C1 (§12.4): scope resolution chain in the spawn tool --------------------


def _scoped_spawn_setup(monkeypatch, tmp_path):
    """Wire the handler to a tmp AGENT_DATA_DIR with real stores; capture spawn."""

    import app.tools.handlers.subagent as handler_mod
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    captured: dict = {}

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["ctx"] = ctx
        captured["spec"] = spec
        captured["task"] = task
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="digest", tokens_used=7),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)
    return handler_mod, captured


@pytest.mark.asyncio
async def test_spawn_tool_agent_definition_wins_and_memory_follows(monkeypatch, tmp_path):
    from app.agents.subagent_definition import definition_store_for_agent, definition_store_for_tenant

    handler_mod, captured = _scoped_spawn_setup(monkeypatch, tmp_path)
    tenant_id = str(uuid.uuid4())
    request = _tool_request({"task": "scout", "definition_name": "dup"}, tenant_id=tenant_id)
    agent_id = request.context.agent_id

    definition_store_for_tenant(tenant_id, agent_data_dir=tmp_path).save(
        SubagentSpec(name="dup", description="d", type="explorer", system_prompt="tenant version")
    )
    definition_store_for_agent(agent_id, agent_data_dir=tmp_path).save(
        SubagentSpec(name="dup", description="d", type="explorer", system_prompt="agent version")
    )

    out = await handler_mod.spawn_subagent_tool(request)
    data = json.loads(out)

    assert data["ok"] is True
    assert data["definition_scope"] == "agent"
    assert captured["spec"].system_prompt == "agent version"
    # §12.5: memory follows the definition's scope — agent-private store.
    mem_base = str(captured["ctx"].memory_store.base_dir)
    assert f"{agent_id}/subagents/.memory" in mem_base.replace("\\", "/")


@pytest.mark.asyncio
async def test_spawn_tool_tenant_fallback_memory_stays_tenant(monkeypatch, tmp_path):
    from app.agents.subagent_definition import definition_store_for_tenant

    handler_mod, captured = _scoped_spawn_setup(monkeypatch, tmp_path)
    tenant_id = str(uuid.uuid4())
    request = _tool_request({"task": "scout", "definition_name": "shared"}, tenant_id=tenant_id)

    definition_store_for_tenant(tenant_id, agent_data_dir=tmp_path).save(
        SubagentSpec(name="shared", description="d", type="explorer", system_prompt="tenant shared")
    )

    out = await handler_mod.spawn_subagent_tool(request)
    data = json.loads(out)

    assert data["ok"] is True
    assert data["definition_scope"] == "tenant"
    assert captured["spec"].system_prompt == "tenant shared"
    mem_base = str(captured["ctx"].memory_store.base_dir)
    assert "_tenants" in mem_base and "/subagents/memory" in mem_base.replace("\\", "/")


@pytest.mark.asyncio
async def test_spawn_tool_definition_not_found_lists_both_scopes(monkeypatch, tmp_path):
    from app.agents.subagent_definition import definition_store_for_agent, definition_store_for_tenant

    handler_mod, _captured = _scoped_spawn_setup(monkeypatch, tmp_path)
    tenant_id = str(uuid.uuid4())
    request = _tool_request({"task": "scout", "definition_name": "ghost"}, tenant_id=tenant_id)
    agent_id = request.context.agent_id

    definition_store_for_agent(agent_id, agent_data_dir=tmp_path).save(
        SubagentSpec(name="mine", description="d", type="explorer", system_prompt="agent def")
    )
    definition_store_for_tenant(tenant_id, agent_data_dir=tmp_path).save(
        SubagentSpec(name="ours", description="d", type="critic", system_prompt="tenant def")
    )

    out = await handler_mod.spawn_subagent_tool(request)
    data = json.loads(out)

    assert data["ok"] is False
    assert "ghost" in data["error"]
    available = {row["name"]: row["scope"] for row in data["available"]}
    assert available["mine"] == "agent"
    assert available["ours"] == "tenant"
    # Builtin template rows included so the model can self-correct to inline spawn.
    assert available["explorer"] == "builtin"
    # CC parity: every row carries its whenToUse so the model can pick by purpose.
    descriptions = {row["name"]: row["description"] for row in data["available"]}
    assert descriptions["mine"] == "d"
    assert descriptions["explorer"]  # builtin whenToUse is non-empty


@pytest.mark.asyncio
async def test_spawn_tool_agent_definition_resolves_without_tenant(monkeypatch, tmp_path):
    from app.agents.subagent_definition import definition_store_for_agent

    handler_mod, captured = _scoped_spawn_setup(monkeypatch, tmp_path)
    request = _tool_request({"task": "scout", "definition_name": "mine"}, tenant_id=None)
    agent_id = request.context.agent_id

    definition_store_for_agent(agent_id, agent_data_dir=tmp_path).save(
        SubagentSpec(name="mine", description="d", type="explorer", system_prompt="agent def")
    )

    out = await handler_mod.spawn_subagent_tool(request)
    data = json.loads(out)

    assert data["ok"] is True
    assert data["definition_scope"] == "agent"
    assert captured["spec"].system_prompt == "agent def"


# ── T1.3 (§8.1 #5) — ledger_todo_id exposed on the spawn contract ──


def test_spawn_parameters_expose_ledger_todo_id():
    """The service entry has accepted ``ledger_todo_id`` since 切口③ (stamp on
    spawn, write-back on completion); T1.3 exposes it on the tool schema."""
    import app.tools.handlers.subagent as handler_mod

    properties = handler_mod._SPAWN_PARAMETERS["properties"]
    assert "ledger_todo_id" in properties
    assert properties["ledger_todo_id"]["type"] == "string"


@pytest.mark.asyncio
async def test_spawn_tool_threads_ledger_todo_id(monkeypatch):
    import app.tools.handlers.subagent as handler_mod

    captured: dict = {}

    async def fake_resolve(agent_id):
        return (
            SimpleNamespace(provider="openai", api_key="k", model="x", base_url=None),
            None,
            SimpleNamespace(name="HR"),
        )

    async def fake_spawn(ctx, spec, task, **kwargs):
        captured["ledger_todo_id"] = kwargs.get("ledger_todo_id")
        return SubagentHandle(
            name=spec.name,
            trace_id="",
            depth=2,
            result=SubagentResult(name=spec.name, type="explorer", status="completed", content="d", tokens_used=1),
        )

    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fake_resolve)
    monkeypatch.setattr(handler_mod, "spawn_subagent", fake_spawn)

    out = await handler_mod.spawn_subagent_tool(_tool_request({"task": "investigate", "ledger_todo_id": "todo-42"}))
    assert json.loads(out)["ok"] is True
    assert captured["ledger_todo_id"] == "todo-42"
