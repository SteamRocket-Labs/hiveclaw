from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, execute_values):
        self._execute_values = list(execute_values)

    async def execute(self, _query):
        if not self._execute_values:
            raise AssertionError("No fake execute result prepared")
        return _FakeScalarResult(self._execute_values.pop(0))


@pytest.mark.asyncio
async def test_get_agent_reply_delegates_to_runtime_invoker(monkeypatch):
    from app.services.supervision_reminder import _get_agent_reply

    target_agent_id = uuid4()
    creator_id = uuid4()
    model_id = uuid4()
    fallback_model_id = uuid4()
    tenant_id = uuid4()
    target_agent = SimpleNamespace(
        id=target_agent_id,
        name="督办目标Agent",
        role_description="跟进事项",
        creator_id=creator_id,
        primary_model_id=model_id,
        fallback_model_id=fallback_model_id,
        max_tool_rounds=7,
        tenant_id=tenant_id,
    )
    model = SimpleNamespace(
        id=model_id,
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
        tenant_id=tenant_id,
    )
    fallback_model = SimpleNamespace(
        id=fallback_model_id,
        provider="anthropic",
        model="claude-sonnet",
        api_key="fallback-key",
        base_url=None,
        max_output_tokens=None,
        tenant_id=tenant_id,
    )
    db = _FakeDB([model, fallback_model])
    captured = {}

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="已收到，我会跟进")

    monkeypatch.setattr("app.services.supervision_reminder.invoke_agent", fake_invoke_agent)

    reply = await _get_agent_reply(target_agent, "请汇报进度", db)

    assert reply == "已收到，我会跟进"
    request = captured["request"]
    assert request.model is model
    assert request.fallback_model is fallback_model
    assert request.agent_id == target_agent_id
    assert request.user_id == creator_id
    assert request.messages == [{"role": "user", "content": "请汇报进度"}]
    assert request.memory_messages == request.messages
    assert request.core_tools_only is True
    assert request.max_tool_rounds == 7
    assert request.execution_identity is not None
    assert request.execution_identity.identity_type == "agent_bot"
    assert request.execution_identity.identity_id == target_agent_id
    assert request.execution_identity.label == "Agent: 督办目标Agent (supervision)"
    assert request.session_context is not None
    assert request.session_context.source == "supervision"
    assert request.session_context.channel == "supervision"


def test_supervision_reminder_uses_unified_runtime_surface():
    project_root = Path(__file__).resolve().parents[3]
    source = (project_root / "backend/app/services/supervision_reminder.py").read_text(encoding="utf-8")

    assert "build_agent_context(" not in source
    assert "create_llm_client(" not in source
    assert 'LLMMessage(role="system"' not in source
