"""Goal Mode agent tools: goal_start (A9) / update_goal (A1) / get_goal (A5)."""

from __future__ import annotations

import importlib
import json
from uuid import uuid4

import pytest


def _collect():
    from app.tools.collector import HANDLER_MODULES, collect_tools
    from app.tools.decorator import clear_registry

    clear_registry()
    for module_name in HANDLER_MODULES:
        importlib.reload(importlib.import_module(module_name))
    return collect_tools()


def _request(tool_name, arguments, tmp_path, *, agent_id=None, user_id=None, tenant_id=None, session_id=None):
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    return ToolExecutionRequest(
        tool_name=tool_name,
        arguments=arguments,
        context=ToolExecutionContext(
            agent_id=agent_id or uuid4(),
            user_id=user_id or uuid4(),
            tenant_id=str(tenant_id or uuid4()),
            workspace=tmp_path,
            session_id=str(session_id or uuid4()),
        ),
    )


def test_goal_mode_tools_are_registered():
    collected = _collect()
    names = {tool["function"]["name"] for tool in collected.openai_tools}
    assert {"goal_start", "update_goal", "get_goal"} <= names
    assert "get_goal" in collected.read_only_names
    assert "update_goal" not in collected.read_only_names


def test_goal_mode_tools_are_capability_mapped():
    from app.services.governance_capability_taxonomy import CAPABILITY_MAP, CORE_TOOL_NAMES

    assert CAPABILITY_MAP.get("goal_start") == "agent.goal.modify"
    assert CAPABILITY_MAP.get("update_goal") == "agent.goal.modify"
    assert CAPABILITY_MAP.get("get_goal") == "agent.goal.read"
    assert {"goal_start", "update_goal", "get_goal"} <= CORE_TOOL_NAMES


def test_capability_audit_has_no_drift_after_goal_tools():
    _collect()
    from app.services.capability_gate import audit_capability_mapping

    drift = audit_capability_mapping()
    assert drift == {"unmapped": [], "stale": []}


@pytest.mark.asyncio
async def test_goal_start_tool_persists_and_returns_goal_id(tmp_path, monkeypatch):
    from app.tools.handlers import command_parity

    agent_id = uuid4()
    session_id = uuid4()
    calls: list[dict] = []

    async def fake_persist(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "goal_id": "goal-123",
            "superseded_goal_id": None,
            "objective": kwargs["objective"],
            "token_budget": kwargs.get("token_budget"),
            "max_continuation_turns": kwargs.get("max_continuation_turns"),
            "status": "active",
        }

    monkeypatch.setattr(command_parity, "persist_session_goal_from_tool", fake_persist, raising=False)

    result = await command_parity.goal_start(
        _request(
            "goal_start",
            {"objective": "Finish parity", "token_budget": 4000, "max_continuation_turns": 5},
            tmp_path,
            agent_id=agent_id,
            session_id=session_id,
        )
    )

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["goal_id"] == "goal-123"
    # A9: it actually persists now, so requires_api_persist flips to False.
    assert payload["requires_api_persist"] is False
    assert calls and calls[0]["objective"] == "Finish parity"
    assert calls[0]["agent_id"] == agent_id
    assert str(calls[0]["session_id"]) == str(session_id)
    assert calls[0]["token_budget"] == 4000
    assert calls[0]["max_continuation_turns"] == 5


@pytest.mark.asyncio
async def test_goal_start_tool_requires_objective(tmp_path):
    from app.tools.handlers.command_parity import goal_start

    payload = json.loads(await goal_start(_request("goal_start", {"objective": "   "}, tmp_path)))
    assert payload["ok"] is False
    assert "objective" in payload["error"]


@pytest.mark.asyncio
async def test_goal_start_tool_requires_session(tmp_path, monkeypatch):
    from app.tools.handlers import command_parity
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    request = ToolExecutionRequest(
        tool_name="goal_start",
        arguments={"objective": "No session"},
        context=ToolExecutionContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            workspace=tmp_path,
            session_id=None,
        ),
    )
    payload = json.loads(await command_parity.goal_start(request))
    assert payload["ok"] is False
    assert "session" in payload["error"].lower()


@pytest.mark.asyncio
async def test_update_goal_tool_marks_complete(tmp_path, monkeypatch):
    from app.tools.handlers import command_parity

    agent_id = uuid4()
    session_id = uuid4()
    calls: list[dict] = []

    async def fake_update(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "goal_id": "goal-1", "status": "complete", "objective": "Done"}

    monkeypatch.setattr(command_parity, "update_session_goal_from_tool", fake_update, raising=False)

    result = await command_parity.update_goal(
        _request(
            "update_goal",
            {"status": "complete", "summary": "shipped"},
            tmp_path,
            agent_id=agent_id,
            session_id=session_id,
        )
    )

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["status"] == "complete"
    assert calls[0]["status"] == "complete"
    assert calls[0]["summary"] == "shipped"
    assert calls[0]["agent_id"] == agent_id


@pytest.mark.asyncio
async def test_update_goal_tool_rejects_unknown_status(tmp_path):
    from app.tools.handlers.command_parity import update_goal

    payload = json.loads(await update_goal(_request("update_goal", {"status": "banana"}, tmp_path)))
    assert payload["ok"] is False
    assert "status" in payload["error"].lower()


@pytest.mark.asyncio
async def test_update_goal_tool_requires_a_field(tmp_path):
    from app.tools.handlers.command_parity import update_goal

    payload = json.loads(await update_goal(_request("update_goal", {}, tmp_path)))
    assert payload["ok"] is False


@pytest.mark.asyncio
async def test_get_goal_tool_returns_state(tmp_path, monkeypatch):
    from app.tools.handlers import command_parity

    async def fake_get(**kwargs):
        return {
            "ok": True,
            "objective": "Track",
            "status": "active",
            "tokens_used": 100,
            "token_budget": 500,
            "remaining_tokens": 400,
            "continuation_count": 1,
        }

    monkeypatch.setattr(command_parity, "get_session_goal_from_tool", fake_get, raising=False)

    result = await command_parity.get_goal(_request("get_goal", {}, tmp_path, session_id=uuid4()))
    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["objective"] == "Track"
    assert payload["remaining_tokens"] == 400
