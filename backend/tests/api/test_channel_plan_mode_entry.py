from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _QueuedDB:
    def __init__(self, values):
        self.values = list(values)
        self.execute_calls = 0

    async def execute(self, _stmt):
        self.execute_calls += 1
        if not self.values:
            raise AssertionError("Unexpected execute() call")
        return _ScalarResult(self.values.pop(0))

    async def commit(self):
        return None


def _agent(**overrides):
    data = {
        "id": uuid4(),
        "name": "Channel Agent",
        "role_description": "Assistant",
        "tenant_id": None,
        "primary_model_id": None,
        "fallback_model_id": None,
        "expires_at": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_channel_llm_recommends_plan_mode_for_schedule_intent():
    from app.api.feishu import _call_agent_llm

    agent = _agent()
    db = _QueuedDB([agent])

    reply = await _call_agent_llm(db, agent.id, "每天 9 点提醒我看 X 上的帖子")

    assert "建议先进入计划模式" in reply
    assert "不用计划模式，直接创建" in reply
    assert db.execute_calls == 1


@pytest.mark.asyncio
async def test_channel_llm_auto_creates_plan_for_long_task(monkeypatch):
    from app.api.feishu import _call_agent_llm

    class _PlanService:
        def __init__(self):
            self.calls = []

        async def ensure_awaiting_plan(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(id=uuid4())

    plan_service = _PlanService()
    monkeypatch.setattr("app.services.plan_mode_service.get_plan_mode_service", lambda: plan_service)
    agent = _agent()
    db = _QueuedDB([agent])

    reply = await _call_agent_llm(db, agent.id, "完整调研这个行业并出报告")

    assert "已进入计划模式" in reply
    assert plan_service.calls[0]["action_kind"] == "start_long_task"
    assert plan_service.calls[0]["tool_name"] == "manage_tasks"
    assert db.execute_calls == 1


@pytest.mark.asyncio
async def test_channel_llm_decline_injects_plan_opt_out_suffix(monkeypatch):
    from app.api.feishu import _call_agent_llm

    captured = {}

    async def fake_call_llm(*_args, **kwargs):
        captured["system_prompt_suffix"] = kwargs.get("system_prompt_suffix")
        return "OK"

    monkeypatch.setattr("app.api.websocket.call_llm", fake_call_llm)
    model_id = uuid4()
    agent = _agent(primary_model_id=model_id)
    model = SimpleNamespace(id=model_id, provider="openai", model="test", supports_vision=False)
    db = _QueuedDB([agent, model])

    reply = await _call_agent_llm(db, agent.id, "不用计划模式，直接创建这个每天 9 点运行的任务")

    assert reply == "OK"
    assert 'plan_mode_decision: "declined"' in captured["system_prompt_suffix"]
