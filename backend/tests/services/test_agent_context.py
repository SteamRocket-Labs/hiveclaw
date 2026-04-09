from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: self._value or [])


class _FakeSession:
    def __init__(self, execute_values):
        self._execute_values = list(execute_values)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _query):
        if not self._execute_values:
            return _FakeScalarResult(None)
        return _FakeScalarResult(self._execute_values.pop(0))


@pytest.mark.asyncio
async def test_build_agent_context_limits_confirmation_rule_to_conversation_mode(monkeypatch, tmp_path):
    from app.services.agent_context import build_agent_context

    agent_id = uuid4()
    sessions = [_FakeSession([[]]), _FakeSession([None])]

    monkeypatch.setattr("app.database.async_session", lambda: sessions.pop(0))
    monkeypatch.setattr("app.services.agent_context.TOOL_WORKSPACE", tmp_path)
    monkeypatch.setattr("app.services.agent_context.PERSISTENT_DATA", tmp_path)
    monkeypatch.setattr("app.services.agent_context._load_skills_index", lambda *_args, **_kwargs: "")

    conversation_prompt = await build_agent_context(
        agent_id,
        "Ops Agent",
        include_runtime_metadata=False,
        include_focus=False,
        execution_mode="conversation",
    )

    sessions = [_FakeSession([[]]), _FakeSession([None])]
    monkeypatch.setattr("app.database.async_session", lambda: sessions.pop(0))

    task_prompt = await build_agent_context(
        agent_id,
        "Ops Agent",
        include_runtime_metadata=False,
        include_focus=False,
        execution_mode="task",
    )

    assert "confirm with the user first" in conversation_prompt
    assert "confirm with the user first" not in task_prompt
    assert "executing an assigned task autonomously" in task_prompt


@pytest.mark.asyncio
async def test_build_agent_context_limits_confirmation_rule_to_heartbeat_mode(monkeypatch, tmp_path):
    from app.services.agent_context import build_agent_context

    agent_id = uuid4()
    sessions = [_FakeSession([[]]), _FakeSession([None])]

    monkeypatch.setattr("app.database.async_session", lambda: sessions.pop(0))
    monkeypatch.setattr("app.services.agent_context.TOOL_WORKSPACE", tmp_path)
    monkeypatch.setattr("app.services.agent_context.PERSISTENT_DATA", tmp_path)
    monkeypatch.setattr("app.services.agent_context._load_skills_index", lambda *_args, **_kwargs: "")

    heartbeat_prompt = await build_agent_context(
        agent_id,
        "Ops Agent",
        include_runtime_metadata=False,
        include_focus=False,
        execution_mode="heartbeat",
    )

    assert "confirm with the user first" not in heartbeat_prompt
    assert "self-evolution mode" in heartbeat_prompt


@pytest.mark.asyncio
async def test_build_agent_context_keeps_confirmation_rule_for_coordinator_mode(monkeypatch, tmp_path):
    from app.services.agent_context import build_agent_context

    agent_id = uuid4()
    sessions = [_FakeSession([[]]), _FakeSession([None])]

    monkeypatch.setattr("app.database.async_session", lambda: sessions.pop(0))
    monkeypatch.setattr("app.services.agent_context.TOOL_WORKSPACE", tmp_path)
    monkeypatch.setattr("app.services.agent_context.PERSISTENT_DATA", tmp_path)
    monkeypatch.setattr("app.services.agent_context._load_skills_index", lambda *_args, **_kwargs: "")

    coordinator_prompt = await build_agent_context(
        agent_id,
        "Ops Agent",
        include_runtime_metadata=False,
        include_focus=False,
        execution_mode="coordinator",
    )

    assert "confirm with the user first" in coordinator_prompt
    assert "operating in coordinator mode" in coordinator_prompt
