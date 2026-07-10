"""§2 Goal finalization: summary_only budget runs schedule exactly one
summarizing turn, then seal the lane (hard_stopped).

Design: docs/hook-goal-session-expression-plan-2026-07-09.md §2. The five pins:
① exactly-once scheduling ② the finalization prompt carries the no-amplification
contract ③ after the summary the run is hard_stopped with no further wakes
④ amplification stays denied throughout (budget-service tests) ⑤ update_goal /
goal fallback records the terminal state.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.session_goal_runtime import GoalStatus


class _SeqDB:
    """Fake AsyncSession returning canned scalar results in call order."""

    def __init__(self, results):
        self._results = list(results)
        self.flushes = 0

    async def execute(self, _stmt):
        value = self._results.pop(0) if self._results else None
        return SimpleNamespace(
            scalar_one_or_none=lambda: value,
            scalars=lambda: SimpleNamespace(first=lambda: value, all=lambda: [value] if value else []),
        )

    async def flush(self):
        self.flushes += 1


class _FakeBudgetService:
    def __init__(self, run_status="summary_only", cas_results=None):
        self.run_status = run_status
        self.cas_results = list(cas_results or [True])
        self.cas_calls: list[dict] = []
        self.hard_stops: list[dict] = []

    async def get_run(self, *, tenant_id, budget_run_id):
        return SimpleNamespace(id=budget_run_id, status=self.run_status, tenant_id=tenant_id)

    async def mark_summary_turn_state(self, *, tenant_id, budget_run_id, expected_states, new_state, extra=None):
        self.cas_calls.append({"expected": expected_states, "new_state": new_state, "extra": dict(extra or {})})
        if self.cas_results:
            return self.cas_results.pop(0)
        return False

    async def hard_stop_run(self, *, tenant_id, budget_run_id, reason, actor="runtime_budget_breaker"):
        self.hard_stops.append({"budget_run_id": budget_run_id, "reason": reason, "actor": actor})
        return SimpleNamespace(id=budget_run_id, status="hard_stopped")


def _goal(goal_id=None, status=GoalStatus.ACTIVE.value):
    return SimpleNamespace(
        id=goal_id or uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        chat_session_id=uuid.uuid4(),
        objective="ship the report",
        status=status,
        token_budget=100_000,
        tokens_used=90_000,
        time_budget_seconds=None,
        continuation_count=3,
        max_continuation_turns=None,
        blocked_count=0,
        metadata_json={},
    )


def _actors():
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), name="Agent")
    session = SimpleNamespace(id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4(), display_name="Rocky", username="rocky")
    return agent, session, user


@pytest.mark.asyncio
async def test_summary_only_run_issues_exactly_one_finalization_turn(monkeypatch):
    import app.services.goal_continuation_service as svc

    goal = _goal()
    agent, session, user = _actors()
    budget_run_id = uuid.uuid4()
    fake_service = _FakeBudgetService(run_status="summary_only", cas_results=[True])
    monkeypatch.setattr("app.services.runtime_budget_service.RuntimeBudgetService", lambda: fake_service)

    started_runs: list[dict] = []
    broadcasts: list[tuple] = []

    async def fake_start(**kwargs):
        started_runs.append(kwargs)
        return {"run_id": "summary-run-1", "status": "pending"}

    async def fake_broadcast(agent_id, session_id, event):
        broadcasts.append((agent_id, session_id, event))

    monkeypatch.setattr(svc, "start_web_chat_run", fake_start)
    monkeypatch.setattr(svc, "broadcast_web_chat_event", fake_broadcast)

    db = _SeqDB([goal, agent, session, user])
    result = await svc.maybe_continue_session_goal_after_turn(
        db=db,
        agent_id=agent.id,
        session_id=session.id,
        user_id=user.id,
        completed_task_type="goal_continuation",
        completed_status="completed",
        metadata_json={"budget_run_id": str(budget_run_id)},
    )

    assert result["reason"] == "budget_summary_issued"
    assert len(started_runs) == 1
    kwargs = started_runs[0]
    extra = kwargs["extra_metadata"]
    assert extra["budget_summary_turn"] is True
    assert extra["budget_run_id"] == str(budget_run_id)
    assert extra["goal_id"] == str(goal.id)
    # ② the finalization contract is model-visible.
    prompt = kwargs["content"]
    assert "Do NOT start new work" in prompt
    assert "update_goal" in prompt
    assert "<session_goal>" in prompt
    assert "ship the report" in prompt
    # CAS won from the empty state.
    assert fake_service.cas_calls[0]["expected"] == (None,)
    assert fake_service.cas_calls[0]["new_state"] == "issued"
    # The awaiting_budget phase reached the session stream.
    phase_events = [event for _a, _s, event in broadcasts if event.get("type") == "phase"]
    assert [event["phase"] for event in phase_events] == ["awaiting_budget"]
    # Goal metadata records the issued summary run.
    assert goal.metadata_json["budget_summary_run_id"] == "summary-run-1"


@pytest.mark.asyncio
async def test_summary_wake_never_double_issues_and_never_continues_normally(monkeypatch):
    import app.services.goal_continuation_service as svc

    goal = _goal()
    agent, session, user = _actors()
    fake_service = _FakeBudgetService(run_status="summary_only", cas_results=[False])
    monkeypatch.setattr("app.services.runtime_budget_service.RuntimeBudgetService", lambda: fake_service)

    async def fail_start(**_kwargs):
        raise AssertionError("must not start any run when the lane is already spoken for")

    async def fail_continue(**_kwargs):
        raise AssertionError("must not fall through to a normal continuation on summary_only")

    monkeypatch.setattr(svc, "start_web_chat_run", fail_start)
    monkeypatch.setattr(svc, "continue_session_goal", fail_continue)

    async def noop_broadcast(*_args):
        return None

    monkeypatch.setattr(svc, "broadcast_web_chat_event", noop_broadcast)

    db = _SeqDB([goal, agent, session, user])
    result = await svc.maybe_continue_session_goal_after_turn(
        db=db,
        agent_id=agent.id,
        session_id=session.id,
        user_id=user.id,
        completed_task_type="web_chat_turn",
        completed_status="completed",
        metadata_json={"budget_run_id": str(uuid.uuid4())},
    )
    assert result["reason"] == "budget_summary_already_issued"


@pytest.mark.asyncio
async def test_completed_summary_turn_seals_lane_and_parks_goal(monkeypatch):
    import app.services.goal_continuation_service as svc

    goal = _goal(status=GoalStatus.ACTIVE.value)
    budget_run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    fake_service = _FakeBudgetService(cas_results=[True])
    monkeypatch.setattr("app.services.runtime_budget_service.RuntimeBudgetService", lambda: fake_service)

    db = _SeqDB([goal])
    result = await svc.maybe_continue_session_goal_after_turn(
        db=db,
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        completed_task_type="goal_continuation",
        completed_status="completed",
        metadata_json={
            "budget_summary_turn": True,
            "budget_run_id": str(budget_run_id),
            "budget_summary_tenant_id": str(tenant_id),
            "goal_id": str(goal.id),
        },
    )

    assert result["reason"] == "budget_summary_completed"
    # ③ the lane is sealed for good.
    assert fake_service.hard_stops == [
        {"budget_run_id": budget_run_id, "reason": "budget_summary_completed", "actor": "goal_continuation_summary"}
    ]
    # ⑤ fallback: the model did not call update_goal, so the goal parks at BUDGET_LIMITED.
    assert goal.status == GoalStatus.BUDGET_LIMITED.value
    assert goal.metadata_json["budget_summary_outcome"] == "summary_completed"


@pytest.mark.asyncio
async def test_completed_summary_turn_respects_model_recorded_goal_state(monkeypatch):
    import app.services.goal_continuation_service as svc

    goal = _goal(status=GoalStatus.COMPLETE.value)
    fake_service = _FakeBudgetService(cas_results=[True])
    monkeypatch.setattr("app.services.runtime_budget_service.RuntimeBudgetService", lambda: fake_service)

    db = _SeqDB([goal])
    await svc.maybe_continue_session_goal_after_turn(
        db=db,
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        completed_task_type="goal_continuation",
        completed_status="completed",
        metadata_json={
            "budget_summary_turn": True,
            "budget_run_id": str(uuid.uuid4()),
            "goal_id": str(goal.id),
        },
    )
    # update_goal already terminalized the goal: the fallback must not overwrite it.
    assert goal.status == GoalStatus.COMPLETE.value


@pytest.mark.asyncio
async def test_failed_summary_turn_retries_once_then_seals_with_summary_failed(monkeypatch):
    import app.services.goal_continuation_service as svc

    goal = _goal()
    agent, session, user = _actors()
    budget_run_id = uuid.uuid4()

    # First failure: issued -> retried CAS wins, a retry turn is started.
    retry_service = _FakeBudgetService(cas_results=[True])
    monkeypatch.setattr("app.services.runtime_budget_service.RuntimeBudgetService", lambda: retry_service)
    retry_runs: list[dict] = []

    async def fake_start(**kwargs):
        retry_runs.append(kwargs)
        return {"run_id": "summary-run-2", "status": "pending"}

    monkeypatch.setattr(svc, "start_web_chat_run", fake_start)

    db = _SeqDB([agent, session, user, goal])
    first = await svc.maybe_continue_session_goal_after_turn(
        db=db,
        agent_id=agent.id,
        session_id=session.id,
        user_id=user.id,
        completed_task_type="goal_continuation",
        completed_status="failed",
        metadata_json={
            "budget_summary_turn": True,
            "budget_run_id": str(budget_run_id),
            "goal_id": str(goal.id),
        },
    )
    assert first["reason"] == "budget_summary_retry"
    assert len(retry_runs) == 1
    assert retry_runs[0]["extra_metadata"]["summary_attempt"] == 2
    assert retry_service.hard_stops == []

    # Second failure: issued->retried CAS loses, retried->failed CAS wins, lane seals.
    fail_service = _FakeBudgetService(cas_results=[False, True])
    monkeypatch.setattr("app.services.runtime_budget_service.RuntimeBudgetService", lambda: fail_service)
    broadcasts: list[dict] = []

    async def fake_broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)

    monkeypatch.setattr(svc, "broadcast_web_chat_event", fake_broadcast)

    db2 = _SeqDB([goal])
    second = await svc.maybe_continue_session_goal_after_turn(
        db=db2,
        agent_id=agent.id,
        session_id=session.id,
        user_id=user.id,
        completed_task_type="goal_continuation",
        completed_status="failed",
        metadata_json={
            "budget_summary_turn": True,
            "budget_run_id": str(budget_run_id),
            "goal_id": str(goal.id),
        },
    )
    assert second["reason"] == "budget_summary_failed"
    assert fail_service.hard_stops[0]["reason"] == "budget_summary_failed"
    assert goal.metadata_json["budget_summary_outcome"] == "summary_failed"
    # The failure is surfaced to the session stream for the UI.
    assert any(
        event.get("type") == "runtime_action_failed" and event.get("status") == "summary_failed" for event in broadcasts
    )


@pytest.mark.asyncio
async def test_healthy_budget_run_takes_the_normal_continuation_path(monkeypatch):
    import app.services.goal_continuation_service as svc

    goal = _goal()
    agent, session, user = _actors()
    fake_service = _FakeBudgetService(run_status="active")
    monkeypatch.setattr("app.services.runtime_budget_service.RuntimeBudgetService", lambda: fake_service)

    continued: list[dict] = []

    async def fake_continue(**kwargs):
        continued.append(kwargs)
        return {"ok": True, "goal_id": str(goal.id), "decision": {}}

    monkeypatch.setattr(svc, "continue_session_goal", fake_continue)

    db = _SeqDB([goal, agent, session, user])
    result = await svc.maybe_continue_session_goal_after_turn(
        db=db,
        agent_id=agent.id,
        session_id=session.id,
        user_id=user.id,
        completed_task_type="web_chat_turn",
        completed_status="completed",
        metadata_json={"budget_run_id": str(uuid.uuid4())},
    )
    assert result["ok"] is True
    assert len(continued) == 1
    assert fake_service.cas_calls == []


def test_budget_summary_contract_prompt_pins_the_finalization_contract():
    from app.runtime.prompts.goals import ThreadGoalPromptState, budget_summary_contract_prompt

    prompt = budget_summary_contract_prompt(
        ThreadGoalPromptState(objective="migrate the billing tables", tokens_used=99_000, token_budget=100_000)
    )
    assert "single finalization turn" in prompt
    assert "Do NOT start new work" in prompt
    assert 'update_goal(status="complete"' in prompt
    assert 'update_goal(status="blocked"' in prompt
    assert "<objective>migrate the billing tables</objective>" in prompt
