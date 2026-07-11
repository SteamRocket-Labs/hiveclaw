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
        invocation_scope="conversation",
    )

    sessions = [_FakeSession([[]]), _FakeSession([None])]
    monkeypatch.setattr("app.database.async_session", lambda: sessions.pop(0))

    task_prompt = await build_agent_context(
        agent_id,
        "Ops Agent",
        include_runtime_metadata=False,
        include_focus=False,
        invocation_scope="task",
    )

    # PR-22 quantified the confirmation rule with explicit trigger categories
    # ("Confirm with the user BEFORE any of these actions, every time: …").
    # The safety boundary is preserved; wording evolved.
    assert "Confirm with the user BEFORE" in conversation_prompt
    assert "Confirm with the user BEFORE" not in task_prompt
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
        invocation_scope="heartbeat",
    )

    assert "Confirm with the user BEFORE" not in heartbeat_prompt
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
        invocation_scope="coordinator",
    )

    assert "Confirm with the user BEFORE" in coordinator_prompt
    assert "operating in coordinator mode" in coordinator_prompt


@pytest.mark.asyncio
async def test_build_agent_context_default_excludes_runtime_time_from_frozen_prefix(monkeypatch, tmp_path):
    import app.services.agent_context as agent_context

    agent_id = uuid4()
    sessions = [_FakeSession([[]]), _FakeSession([None])]

    async def fake_runtime_metadata(*_args, **_kwargs):
        return ["\n## Current Time\n2026-06-12 09:30:00 (Asia/Shanghai)"]

    monkeypatch.setattr("app.database.async_session", lambda: sessions.pop(0))
    monkeypatch.setattr("app.services.agent_context.TOOL_WORKSPACE", tmp_path)
    monkeypatch.setattr("app.services.agent_context.PERSISTENT_DATA", tmp_path)
    monkeypatch.setattr("app.services.agent_context._load_skills_index", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(agent_context, "_build_runtime_metadata_sections", fake_runtime_metadata)

    prompt = await agent_context.build_agent_context(
        agent_id,
        "Ops Agent",
        include_focus=False,
        invocation_scope="conversation",
    )

    assert "Current Time" not in prompt
    assert "Asia/Shanghai" not in prompt


@pytest.mark.asyncio
async def test_frozen_context_uses_pinned_tenant_session_for_channel_and_company_reads(monkeypatch, tmp_path):
    import app.services.agent_context as agent_context

    agent_id = uuid4()
    tenant_id = uuid4()
    tenant_calls = []
    sessions = [
        _FakeSession([[SimpleNamespace(channel_type="slack")]]),
        _FakeSession([None, None, None]),
    ]

    def fake_tenant_scoped_session(requested_tenant_id, **_kwargs):
        tenant_calls.append(requested_tenant_id)
        return sessions.pop(0)

    async def no_a2a(*_args, **_kwargs):
        return ""

    monkeypatch.setattr("app.database.tenant_scoped_session", fake_tenant_scoped_session)
    monkeypatch.setattr(
        "app.database.async_session",
        lambda: (_ for _ in ()).throw(AssertionError("frozen context must not use an unscoped DB session")),
    )
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", tmp_path)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", tmp_path)
    monkeypatch.setattr(agent_context, "_build_a2a_collaborators_context", no_a2a)

    prompt = await agent_context.build_agent_context(
        agent_id,
        "Ops Agent",
        tenant_id=tenant_id,
        include_skill_catalog=False,
    )

    assert tenant_calls == [tenant_id, tenant_id]
    assert "You have slack channel(s) configured" in prompt
