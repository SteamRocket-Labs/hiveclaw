from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, execute_values):
        self._execute_values = list(execute_values)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _query):
        if not self._execute_values:
            raise AssertionError("No fake execute result prepared")
        return _FakeScalarResult(self._execute_values.pop(0))


@pytest.mark.asyncio
async def test_execute_schedule_delegates_to_runtime_invoker(monkeypatch):
    from app.services.scheduler import _execute_schedule

    schedule_id = uuid4()
    agent_id = uuid4()
    model_id = uuid4()
    creator_id = uuid4()

    tenant_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Scheduler Agent",
        status="running",
        role_description="Scheduler",
        primary_model_id=model_id,
        fallback_model_id=None,
        creator_id=creator_id,
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
    captured = {}
    activity_calls = []

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="定时执行成功")

    async def fake_log_activity(*args, **kwargs):
        activity_calls.append((args, kwargs))

    monkeypatch.setattr("app.services.scheduler.async_session", lambda: _FakeSession([agent, model]))
    monkeypatch.setattr("app.services.scheduler.invoke_agent", fake_invoke_agent)
    monkeypatch.setattr("app.core.permissions.is_agent_expired", lambda _agent: False)
    monkeypatch.setattr("app.services.activity_logger.log_activity", fake_log_activity)

    await _execute_schedule(schedule_id, agent_id, "生成日报")

    request = captured["request"]
    assert request.model is model
    assert request.agent_id == agent_id
    assert request.user_id == creator_id
    assert request.core_tools_only is True
    assert request.messages == [{"role": "user", "content": "[自动调度任务] 生成日报"}]
    assert request.memory_messages == request.messages
    assert request.session_context is not None
    assert request.session_context.source == "schedule"
    assert request.session_context.channel == "schedule"
    assert request.session_context.metadata["schedule_id"] == str(schedule_id)
    assert request.execution_identity is not None
    assert request.execution_identity.identity_type == "agent_bot"
    assert request.execution_identity.identity_id == agent_id
    assert request.execution_identity.label == "Agent: Scheduler Agent (schedule)"
    assert activity_calls
