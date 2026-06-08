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
async def test_channel_llm_does_not_auto_enter_plan_mode_for_long_task(monkeypatch):
    """A (user correction): pure long-task wording must NOT auto-enter Plan Mode in
    a channel either. The agent's judgment never triggers entry — the turn falls
    through to normal execution (here the no-model notice, since the test agent has
    no LLM configured) and no plan is auto-authored."""
    from app.api.feishu import _call_agent_llm

    launched: list = []

    async def fake_launch(plan, *, seed_context=None):
        launched.append(plan)
        return plan

    monkeypatch.setattr("app.services.plan_mode_system_run.launch_system_plan_run", fake_launch)

    agent = _agent()
    db = _QueuedDB([agent])

    reply = await _call_agent_llm(db, agent.id, "完整调研这个行业并出报告")

    assert "已进入计划模式" not in reply  # long-task text no longer auto-enters
    assert launched == []  # no plan auto-authored
    assert db.execute_calls == 1


@pytest.mark.asyncio
async def test_channel_llm_accepts_latest_recommendation_instead_of_reclassifying(monkeypatch):
    from app.api.feishu import _call_agent_llm

    draft = SimpleNamespace(id=uuid4())
    launched: list = []

    class _PlanService:
        async def create_plan_request(self, **kwargs):
            self.create_kwargs = kwargs
            return draft

        async def get_plan(self, _plan_id):
            return SimpleNamespace(id=draft.id, status="awaiting_confirmation")

    plan_service = _PlanService()
    monkeypatch.setattr("app.services.plan_mode_service.get_plan_mode_service", lambda: plan_service)

    async def fake_launch(plan, *, seed_context=None):
        launched.append({"plan_id": plan.id, "seed_context": seed_context})
        return plan

    monkeypatch.setattr("app.services.plan_mode_system_run.launch_system_plan_run", fake_launch)
    agent = _agent()
    user_id = uuid4()
    recommendation = SimpleNamespace(
        id=uuid4(),
        agent_id=agent.id,
        session_id="session-accept",
        recommended_to_user_id=user_id,
        status="recommended",
        original_request="每天 13:00 自动检查 Reddit 帖子并总结投资观点",
        title="每天 13:00 自动检查 Reddit 帖子",
        intent_type="autonomous_wake",
        action_kind="create_enabled_trigger",
        tool_name="set_trigger",
        accepted_by_user_id=None,
        accepted_at=None,
    )
    db = _QueuedDB([agent, recommendation])

    reply = await _call_agent_llm(
        db,
        agent.id,
        "进入计划模式",
        user_id=user_id,
        session_id="session-accept",
    )

    assert "已进入计划模式" in reply
    assert recommendation.status == "accepted"
    assert recommendation.accepted_by_user_id == user_id
    # Cut ④: the accepted recommendation drives a main-loop Plan Mode run; the
    # classified action + args are carried as the launcher's seed context.
    assert len(launched) == 1
    seed = launched[0]["seed_context"]
    assert seed["action_kind"] == "create_enabled_trigger"
    assert seed["tool_name"] == "set_trigger"
    assert seed["arguments"]["reason"] == recommendation.original_request
    assert "每天 13:00" in seed["arguments"]["name"]
    assert plan_service.create_kwargs["original_request"] == recommendation.original_request


@pytest.mark.asyncio
async def test_channel_llm_confirms_latest_awaiting_plan_from_text_and_handoffs(monkeypatch):
    from app.api.feishu import _call_agent_llm

    user_id = uuid4()
    plan = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        session_id="wechat-session",
        status="awaiting_confirmation",
        plan_version=3,
        plan_hash="sha256:abc",
        handoff_status=None,
        handoff_payload=None,
    )

    class _PlanService:
        def __init__(self):
            self.confirm_calls = []
            self.handoff_calls = []

        async def find_latest_awaiting_plan_for_session(self, **kwargs):
            assert kwargs["agent_id"] == plan.agent_id
            assert kwargs["session_id"] == "wechat-session"
            return plan

        async def confirm_plan(self, **kwargs):
            self.confirm_calls.append(kwargs)
            plan.status = "confirmed"
            plan.confirmed_by_user_id = kwargs["confirming_user_id"]
            plan.handoff_status = "not_started"
            return plan

        async def handoff_confirmed_plan(self, **kwargs):
            self.handoff_calls.append(kwargs)
            plan.handoff_status = "completed"
            plan.handoff_payload = {"runtime_task_id": "rt-1"}
            return plan

    async def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("LLM should not be invoked for a trusted text plan confirmation")

    plan_service = _PlanService()
    monkeypatch.setattr("app.services.plan_mode_service.get_plan_mode_service", lambda: plan_service)
    monkeypatch.setattr("app.api.websocket.call_llm", fail_call_llm)
    agent = _agent(id=plan.agent_id)
    db = _QueuedDB([agent])

    reply = await _call_agent_llm(
        db,
        agent.id,
        "确认上一个计划",
        user_id=user_id,
        session_id="wechat-session",
        session_source="wechat_personal",
        session_channel="wechat_personal",
    )

    assert "已确认计划" in reply
    assert "已启动执行" in reply
    assert plan_service.confirm_calls == [
        {
            "plan_id": plan.id,
            "confirming_user_id": user_id,
            "plan_version": 3,
            "plan_hash": "sha256:abc",
            "reason": "confirmed via wechat_personal text",
        }
    ]
    assert plan_service.handoff_calls == [{"plan_id": plan.id}]
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
