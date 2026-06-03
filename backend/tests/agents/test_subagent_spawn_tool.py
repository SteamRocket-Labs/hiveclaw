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


def _tool_request(arguments: dict, *, session_id: str | None = "sess-1") -> ToolExecutionRequest:
    context = ToolExecutionContext(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tenant_id=None,
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
        return SimpleNamespace(model="x"), None, SimpleNamespace(name="HR")

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
