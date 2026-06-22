from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeDB:
    def __init__(self) -> None:
        self.flushes = 0

    async def flush(self):
        self.flushes += 1


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ExecuteDB(_FakeDB):
    def __init__(self, values) -> None:
        super().__init__()
        self.values = list(values)
        self.execute_count = 0

    async def execute(self, _stmt):
        self.execute_count += 1
        if not self.values:
            raise AssertionError("unexpected execute() call")
        return _ScalarResult(self.values.pop(0))


@pytest.mark.asyncio
async def test_continue_session_goal_starts_goal_continuation_run(monkeypatch):
    import app.services.goal_continuation_service as service
    from app.models.agent_session_goal import AgentSessionGoal

    agent_id = uuid4()
    session_id = uuid4()
    user_id = uuid4()
    goal = AgentSessionGoal(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=agent_id,
        chat_session_id=session_id,
        created_by_user_id=user_id,
        objective="Finish the parity implementation.",
        max_continuation_turns=3,
    )
    db = _FakeDB()
    calls: list[dict] = []

    async def fake_start_web_chat_run(**kwargs):
        calls.append(kwargs)
        return {"run_id": "run-1", "status": "running"}

    monkeypatch.setattr(service, "start_web_chat_run", fake_start_web_chat_run)

    result = await service.continue_session_goal(
        db=db,
        agent=SimpleNamespace(id=agent_id, name="Agent", tenant_id=goal.tenant_id),
        user=SimpleNamespace(id=user_id),
        session=SimpleNamespace(id=session_id),
        goal=goal,
    )

    assert result["ok"] is True
    assert result["run"]["run_id"] == "run-1"
    assert goal.continuation_count == 1
    assert goal.metadata_json["last_continuation_run_id"] == "run-1"
    assert calls[0]["runtime_task_type"] == "goal_continuation"
    assert calls[0]["append_user_message"] is False
    assert calls[0]["extra_metadata"]["goal_id"] == str(goal.id)
    assert "Finish the parity implementation." in calls[0]["content"]
    assert db.flushes == 1


@pytest.mark.asyncio
async def test_continue_session_goal_marks_budget_limited_without_starting_run(monkeypatch):
    import app.services.goal_continuation_service as service
    from app.models.agent_session_goal import AgentSessionGoal

    goal = AgentSessionGoal(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=uuid4(),
        chat_session_id=uuid4(),
        objective="Already spent.",
        token_budget=10,
        tokens_used=10,
    )
    db = _FakeDB()

    async def fail_start_web_chat_run(**_kwargs):
        raise AssertionError("budget-limited goals must not start a continuation run")

    monkeypatch.setattr(service, "start_web_chat_run", fail_start_web_chat_run)

    result = await service.continue_session_goal(
        db=db,
        agent=SimpleNamespace(id=goal.agent_id, name="Agent", tenant_id=goal.tenant_id),
        user=SimpleNamespace(id=uuid4()),
        session=SimpleNamespace(id=goal.chat_session_id),
        goal=goal,
    )

    assert result["ok"] is False
    assert result["decision"]["next_status"] == "budget_limited"
    assert goal.status == "budget_limited"
    assert db.flushes == 1


@pytest.mark.asyncio
async def test_maybe_continue_session_goal_after_turn_dispatches_active_goal(monkeypatch):
    import app.services.goal_continuation_service as service
    from app.models.agent_session_goal import AgentSessionGoal

    tenant_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    user_id = uuid4()
    goal = AgentSessionGoal(
        id=uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        chat_session_id=session_id,
        created_by_user_id=user_id,
        objective="Finish active goal.",
        status="active",
    )
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    session = SimpleNamespace(id=session_id)
    user = SimpleNamespace(id=user_id)
    db = _ExecuteDB([goal, agent, session, user])
    calls: list[dict] = []

    async def fake_continue_session_goal(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "goal_id": str(goal.id), "run": {"run_id": "run-2"}}

    monkeypatch.setattr(service, "continue_session_goal", fake_continue_session_goal)

    result = await service.maybe_continue_session_goal_after_turn(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        user_id=user_id,
        completed_task_type="web_chat_turn",
        completed_status="completed",
        metadata_json={"ephemeral": False},
    )

    assert result["ok"] is True
    assert result["goal_id"] == str(goal.id)
    assert db.execute_count == 4
    assert calls[0]["agent"] is agent
    assert calls[0]["session"] is session
    assert calls[0]["user"] is user
    assert calls[0]["goal"] is goal
    assert calls[0]["active_run_exists"] is False
    assert calls[0]["pending_user_input"] is False
    assert calls[0]["plan_mode"] is False


@pytest.mark.asyncio
async def test_maybe_continue_session_goal_after_turn_skips_non_user_turns(monkeypatch):
    import app.services.goal_continuation_service as service

    async def fail_continue_session_goal(**_kwargs):
        raise AssertionError("goal continuation turns must not recursively continue themselves")

    monkeypatch.setattr(service, "continue_session_goal", fail_continue_session_goal)

    db = _ExecuteDB([])
    result = await service.maybe_continue_session_goal_after_turn(
        db=db,
        agent_id=uuid4(),
        session_id=uuid4(),
        user_id=uuid4(),
        completed_task_type="goal_continuation",
        completed_status="completed",
        metadata_json={"source": "goal_continuation"},
    )

    assert result["ok"] is False
    assert result["reason"] == "unsupported_task_type"
    assert db.execute_count == 0
