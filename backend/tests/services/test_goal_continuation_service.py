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
    decision_entry = goal.metadata_json["goal_decision_ledger"][-1]
    assert decision_entry["previous_terminal_reason"] is None
    assert decision_entry["continue_reason"] == "active goal may continue"
    assert decision_entry["stop_reason"] is None
    assert decision_entry["status_transition"] == {"from": "active", "to": "active"}
    assert decision_entry["user_visible_next_action"] == "continuation_scheduled"
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
    decision_entry = goal.metadata_json["goal_decision_ledger"][-1]
    assert decision_entry["stop_reason"] == "token budget exhausted"
    assert decision_entry["user_visible_next_action"] == "show_budget_limit_prompt"
    assert db.flushes == 1


@pytest.mark.asyncio
async def test_continue_session_goal_records_previous_tool_budget_as_usage_limited(monkeypatch):
    import app.services.goal_continuation_service as service
    from app.models.agent_session_goal import AgentSessionGoal

    goal = AgentSessionGoal(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=uuid4(),
        chat_session_id=uuid4(),
        objective="Continue only when the prior turn made progress.",
        status="active",
    )
    db = _FakeDB()

    async def fail_start_web_chat_run(**_kwargs):
        raise AssertionError("tool-budget-limited turns must wait for the user")

    monkeypatch.setattr(service, "start_web_chat_run", fail_start_web_chat_run)

    result = await service.continue_session_goal(
        db=db,
        agent=SimpleNamespace(id=goal.agent_id, name="Agent", tenant_id=goal.tenant_id),
        user=SimpleNamespace(id=uuid4()),
        session=SimpleNamespace(id=goal.chat_session_id),
        goal=goal,
        previous_terminal_reason="tool_budget",
        progress_evidence=["terminal_reason:tool_budget"],
    )

    assert result["ok"] is False
    assert result["decision"]["next_status"] == "usage_limited"
    assert goal.status == "usage_limited"
    decision_entry = goal.metadata_json["goal_decision_ledger"][-1]
    assert decision_entry["previous_terminal_reason"] == "tool_budget"
    assert decision_entry["progress_evidence"] == ["terminal_reason:tool_budget"]
    assert decision_entry["stop_reason"] == "previous turn reached tool budget"
    assert decision_entry["status_transition"] == {"from": "active", "to": "usage_limited"}
    assert decision_entry["user_visible_next_action"] == "ask_user_to_continue"
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
        metadata_json={"ephemeral": False, "terminal_reason": "turn_stop", "artifact_ids": ["artifact-1"]},
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
    assert calls[0]["previous_terminal_reason"] == "turn_stop"
    assert calls[0]["progress_evidence"] == ["terminal_reason:turn_stop", "artifact:artifact-1"]


@pytest.mark.asyncio
async def test_maybe_continue_dispatches_completed_goal_continuation_turn(monkeypatch):
    """A3 autonomous loop: a finished goal_continuation turn feeds the next
    continuation (idle -> continue until a terminal state). The Codex-aligned
    stops are should_continue_goal's cap/budget/blocked/terminal mapping plus
    the inherited budget-plane run — not a one-step-per-user-turn gate."""
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
        objective="Keep going autonomously.",
        status="active",
        continuation_count=2,
        max_continuation_turns=5,
    )
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    session = SimpleNamespace(id=session_id)
    user = SimpleNamespace(id=user_id)
    db = _ExecuteDB([goal, agent, session, user])
    calls: list[dict] = []

    async def fake_continue_session_goal(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "goal_id": str(goal.id), "run": {"run_id": "run-3"}}

    monkeypatch.setattr(service, "continue_session_goal", fake_continue_session_goal)

    result = await service.maybe_continue_session_goal_after_turn(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        user_id=user_id,
        completed_task_type="goal_continuation",
        completed_status="completed",
        metadata_json={"source": "goal_continuation", "terminal_reason": "turn_stop"},
    )

    assert result["ok"] is True
    assert calls and calls[0]["goal"] is goal


@pytest.mark.asyncio
async def test_autonomous_loop_stops_at_continuation_cap(monkeypatch):
    """A3 breaker path: the loop must terminate through should_continue_goal's
    cap once continuation_count reaches max_continuation_turns."""
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
        objective="Bounded autonomy.",
        status="active",
        continuation_count=5,
        max_continuation_turns=5,
    )
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    session = SimpleNamespace(id=session_id)
    user = SimpleNamespace(id=user_id)
    db = _ExecuteDB([goal, agent, session, user])

    result = await service.maybe_continue_session_goal_after_turn(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        user_id=user_id,
        completed_task_type="goal_continuation",
        completed_status="completed",
        metadata_json={"source": "goal_continuation", "terminal_reason": "turn_stop"},
    )

    assert result["ok"] is False
    assert result["decision"]["reason"] == "continuation turn cap reached"


@pytest.mark.asyncio
async def test_maybe_continue_still_skips_unrelated_task_types(monkeypatch):
    import app.services.goal_continuation_service as service

    async def fail_continue_session_goal(**_kwargs):
        raise AssertionError("unrelated task types must not trigger goal continuation")

    monkeypatch.setattr(service, "continue_session_goal", fail_continue_session_goal)

    db = _ExecuteDB([])
    result = await service.maybe_continue_session_goal_after_turn(
        db=db,
        agent_id=uuid4(),
        session_id=uuid4(),
        user_id=uuid4(),
        completed_task_type="workflow",
        completed_status="completed",
        metadata_json={},
    )

    assert result["ok"] is False
    assert result["reason"] == "unsupported_task_type"
    assert db.execute_count == 0


# --- A4: token accounting -------------------------------------------------


@pytest.mark.asyncio
async def test_continue_session_goal_accounts_turn_tokens(monkeypatch):
    import app.services.goal_continuation_service as service
    from app.models.agent_session_goal import AgentSessionGoal

    goal = AgentSessionGoal(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=uuid4(),
        chat_session_id=uuid4(),
        objective="Keep going.",
        token_budget=1000,
        tokens_used=100,
    )
    db = _FakeDB()

    async def fake_start_web_chat_run(**_kwargs):
        return {"run_id": "run-acct", "status": "running"}

    monkeypatch.setattr(service, "start_web_chat_run", fake_start_web_chat_run)

    result = await service.continue_session_goal(
        db=db,
        agent=SimpleNamespace(id=goal.agent_id, name="Agent", tenant_id=goal.tenant_id),
        user=SimpleNamespace(id=uuid4()),
        session=SimpleNamespace(id=goal.chat_session_id),
        goal=goal,
        turn_tokens=250,
    )

    assert result["ok"] is True
    assert goal.tokens_used == 350


@pytest.mark.asyncio
async def test_continue_session_goal_turn_tokens_over_budget_stops(monkeypatch):
    import app.services.goal_continuation_service as service
    from app.models.agent_session_goal import AgentSessionGoal

    goal = AgentSessionGoal(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=uuid4(),
        chat_session_id=uuid4(),
        objective="Almost out of budget.",
        token_budget=100,
        tokens_used=90,
    )
    db = _FakeDB()

    async def fail_start_web_chat_run(**_kwargs):
        raise AssertionError("must not continue once the turn pushed us over budget")

    monkeypatch.setattr(service, "start_web_chat_run", fail_start_web_chat_run)

    result = await service.continue_session_goal(
        db=db,
        agent=SimpleNamespace(id=goal.agent_id, name="Agent", tenant_id=goal.tenant_id),
        user=SimpleNamespace(id=uuid4()),
        session=SimpleNamespace(id=goal.chat_session_id),
        goal=goal,
        turn_tokens=20,
    )

    assert result["ok"] is False
    assert result["decision"]["next_status"] == "budget_limited"
    assert goal.status == "budget_limited"
    assert goal.tokens_used == 110


@pytest.mark.asyncio
async def test_maybe_continue_passes_turn_tokens_from_metadata(monkeypatch):
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
        objective="Account turn tokens.",
        status="active",
    )
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    session = SimpleNamespace(id=session_id)
    user = SimpleNamespace(id=user_id)
    db = _ExecuteDB([goal, agent, session, user])
    calls: list[dict] = []

    async def fake_continue_session_goal(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "goal_id": str(goal.id)}

    monkeypatch.setattr(service, "continue_session_goal", fake_continue_session_goal)

    await service.maybe_continue_session_goal_after_turn(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        user_id=user_id,
        completed_task_type="web_chat_turn",
        completed_status="completed",
        metadata_json={"terminal_reason": "turn_stop", "turn_tokens_used": 512},
    )

    assert calls and calls[0]["turn_tokens"] == 512


# --- A6: consecutive-failure tolerance -> Blocked -------------------------


@pytest.mark.asyncio
async def test_continue_session_goal_retries_provider_error_below_threshold(monkeypatch):
    import app.services.goal_continuation_service as service
    from app.models.agent_session_goal import AgentSessionGoal

    goal = AgentSessionGoal(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=uuid4(),
        chat_session_id=uuid4(),
        objective="Recover from a provider blip.",
        status="active",
        blocked_count=0,
    )
    db = _FakeDB()

    async def fake_start_web_chat_run(**_kwargs):
        return {"run_id": "run-retry", "status": "running"}

    monkeypatch.setattr(service, "start_web_chat_run", fake_start_web_chat_run)

    result = await service.continue_session_goal(
        db=db,
        agent=SimpleNamespace(id=goal.agent_id, name="Agent", tenant_id=goal.tenant_id),
        user=SimpleNamespace(id=uuid4()),
        session=SimpleNamespace(id=goal.chat_session_id),
        goal=goal,
        previous_terminal_reason="provider_error",
    )

    assert result["ok"] is True
    assert goal.blocked_count == 1
    assert goal.status == "active"
    entry = goal.metadata_json["goal_decision_ledger"][-1]
    assert "retry_after_error 1/3" in (entry.get("continue_reason") or "")


@pytest.mark.asyncio
async def test_continue_session_goal_blocks_after_three_consecutive_errors(monkeypatch):
    import app.services.goal_continuation_service as service
    from app.models.agent_session_goal import AgentSessionGoal

    goal = AgentSessionGoal(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=uuid4(),
        chat_session_id=uuid4(),
        objective="Give up after repeated failures.",
        status="active",
        blocked_count=2,
    )
    db = _FakeDB()

    async def fail_start_web_chat_run(**_kwargs):
        raise AssertionError("a goal blocked after repeated errors must not continue")

    monkeypatch.setattr(service, "start_web_chat_run", fail_start_web_chat_run)

    result = await service.continue_session_goal(
        db=db,
        agent=SimpleNamespace(id=goal.agent_id, name="Agent", tenant_id=goal.tenant_id),
        user=SimpleNamespace(id=uuid4()),
        session=SimpleNamespace(id=goal.chat_session_id),
        goal=goal,
        previous_terminal_reason="turn_abort",
    )

    assert result["ok"] is False
    assert goal.blocked_count == 3
    assert goal.status == "blocked"
    assert result["decision"]["next_status"] == "blocked"


@pytest.mark.asyncio
async def test_continue_session_goal_resets_blocked_count_on_clean_turn(monkeypatch):
    import app.services.goal_continuation_service as service
    from app.models.agent_session_goal import AgentSessionGoal

    goal = AgentSessionGoal(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=uuid4(),
        chat_session_id=uuid4(),
        objective="Two fails then a clean turn.",
        status="active",
        blocked_count=2,
    )
    db = _FakeDB()

    async def fake_start_web_chat_run(**_kwargs):
        return {"run_id": "run-clean", "status": "running"}

    monkeypatch.setattr(service, "start_web_chat_run", fake_start_web_chat_run)

    result = await service.continue_session_goal(
        db=db,
        agent=SimpleNamespace(id=goal.agent_id, name="Agent", tenant_id=goal.tenant_id),
        user=SimpleNamespace(id=uuid4()),
        session=SimpleNamespace(id=goal.chat_session_id),
        goal=goal,
        previous_terminal_reason="turn_stop",
    )

    assert result["ok"] is True
    assert goal.blocked_count == 0


# --- A7: objective steering after update ---------------------------------


@pytest.mark.asyncio
async def test_continuation_prompt_injects_objective_steering_when_pending(monkeypatch):
    import app.services.goal_continuation_service as service
    from app.models.agent_session_goal import AgentSessionGoal

    goal = AgentSessionGoal(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=uuid4(),
        chat_session_id=uuid4(),
        objective="Newly re-scoped objective.",
        status="active",
    )
    goal.metadata_json = {"objective_updated_pending": True}
    db = _FakeDB()

    async def fake_start_web_chat_run(**kwargs):
        return {"run_id": "run-steer", "status": "running", "content": kwargs.get("content")}

    monkeypatch.setattr(service, "start_web_chat_run", fake_start_web_chat_run)

    result = await service.continue_session_goal(
        db=db,
        agent=SimpleNamespace(id=goal.agent_id, name="Agent", tenant_id=goal.tenant_id),
        user=SimpleNamespace(id=uuid4()),
        session=SimpleNamespace(id=goal.chat_session_id),
        goal=goal,
    )

    assert result["ok"] is True
    prompt = goal.metadata_json["last_continuation_prompt"]
    assert "updated" in prompt.lower() and "re-orient" in prompt.lower()
    # Steering fires once; the flag is cleared so later turns do not repeat it.
    assert goal.metadata_json.get("objective_updated_pending") is False


# --- A8: resumed turns still qualify for continuation ---------------------


@pytest.mark.asyncio
async def test_maybe_continue_dispatches_resumed_turn(monkeypatch):
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
        objective="Survive a restart.",
        status="active",
    )
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    session = SimpleNamespace(id=session_id)
    user = SimpleNamespace(id=user_id)
    db = _ExecuteDB([goal, agent, session, user])
    calls: list[dict] = []

    async def fake_continue_session_goal(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "goal_id": str(goal.id)}

    monkeypatch.setattr(service, "continue_session_goal", fake_continue_session_goal)

    result = await service.maybe_continue_session_goal_after_turn(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        user_id=user_id,
        completed_task_type="web_chat_turn",
        completed_status="completed",
        metadata_json={
            "terminal_reason": "turn_stop",
            "resumed_after_restart": True,
            "resumed_at": "2026-07-09T00:00:00+00:00",
        },
    )

    assert result["ok"] is True
    assert calls, "a resumed completed web_chat_turn must still trigger goal continuation"
