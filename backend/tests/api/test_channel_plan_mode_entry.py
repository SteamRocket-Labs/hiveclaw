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

    def add(self, _obj):
        return None

    async def flush(self):
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
async def test_channel_llm_decline_without_prior_recommendation_does_not_set_trusted_opt_out(monkeypatch):
    from app.api.feishu import _call_agent_llm

    captured = {}

    async def fake_call_llm(*_args, **kwargs):
        captured["system_prompt_suffix"] = kwargs.get("system_prompt_suffix")
        captured["session_context"] = kwargs.get("session_context")
        return "OK"

    monkeypatch.setattr("app.api.websocket.call_llm", fake_call_llm)
    model_id = uuid4()
    agent = _agent(primary_model_id=model_id)
    model = SimpleNamespace(id=model_id, provider="openai", model="test", supports_vision=False)
    db = _QueuedDB([agent, model])

    reply = await _call_agent_llm(db, agent.id, "不用计划模式，直接创建这个每天 9 点运行的任务")

    assert reply == "OK"
    assert "runtime verified" not in captured["system_prompt_suffix"]
    assert "plan_mode_trusted_user_decline" not in captured["session_context"].metadata


@pytest.mark.asyncio
async def test_channel_llm_decline_after_recommendation_sets_trusted_runtime_opt_out(monkeypatch):
    from app.api.feishu import _call_agent_llm

    captured = {}

    async def fake_call_llm(*_args, **kwargs):
        captured["system_prompt_suffix"] = kwargs.get("system_prompt_suffix")
        captured["session_context"] = kwargs.get("session_context")
        return "OK"

    monkeypatch.setattr("app.api.websocket.call_llm", fake_call_llm)
    model_id = uuid4()
    agent = _agent(primary_model_id=model_id)
    model = SimpleNamespace(id=model_id, provider="openai", model="test", supports_vision=False)
    user_id = uuid4()
    recommendation = SimpleNamespace(
        id=uuid4(),
        agent_id=agent.id,
        session_id="session-1",
        recommended_to_user_id=user_id,
        status="recommended",
        declined_by_user_id=None,
        declined_at=None,
    )
    db = _QueuedDB([agent, model, None, recommendation])
    history = [
        {
            "role": "assistant",
            "content": "这个请求看起来会创建未来自动执行或持续监控：每天 9 点\n\n"
            "建议先进入计划模式，确认执行频率、范围、成本、停止条件和通知方式。"
            "如果你同意，请回复“进入计划模式”；如果你要跳过，请明确回复“不用计划模式，直接创建”。",
        }
    ]

    reply = await _call_agent_llm(
        db,
        agent.id,
        "不用计划模式，直接创建这个每天 9 点运行的任务",
        history=history,
        user_id=user_id,
        session_id="session-1",
    )

    assert reply == "OK"
    assert "runtime verified" in captured["system_prompt_suffix"]
    assert captured["session_context"].metadata["plan_mode_trusted_user_decline"]["reason"] == (
        "user_declined_recommended_plan_mode"
    )
    assert captured["session_context"].metadata["plan_mode_trusted_user_decline"]["recommendation_id"] == str(
        recommendation.id
    )
    assert recommendation.status == "declined"
