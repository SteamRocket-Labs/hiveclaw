from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_session_workbench_aggregates_turn_runtime_goal_and_team_state(monkeypatch):
    import app.services.session_control_plane as service

    agent_id = uuid4()
    session_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    now = datetime(2026, 6, 24, tzinfo=timezone.utc)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        user_id=user_id,
        title="Launch sync",
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
        parent_session_id=None,
        root_session_id=session_id,
        runtime_task_id=None,
        created_at=now,
        last_message_at=now,
    )
    event = SimpleNamespace(
        id=uuid4(),
        event_id=uuid4(),
        sequence=1,
        event_type="user_message",
        actor_type="user",
        role="user",
        content="Ship it",
        metadata_json={"role": "user"},
        created_at=now,
    )
    task = SimpleNamespace(
        id=uuid4(),
        task_type="goal_continuation",
        status="completed",
        parent_agent_id=agent_id,
        child_agent_id=None,
        parent_session_id=str(session_id),
        child_session_id=None,
        trace_id="trace-1",
        created_at=now,
        started_at=now,
        completed_at=now,
        result_summary="done",
        token_usage={"total": 12},
        metadata_json={"source": "goal"},
    )
    goal = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        chat_session_id=session_id,
        objective="Finish",
        status="active",
        token_budget=100,
        tokens_used=12,
        time_budget_seconds=None,
        continuation_count=1,
        max_continuation_turns=3,
        blocked_count=0,
        completion_summary=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )

    async def fake_load_events(db, *, agent, session, limit):
        assert limit == 1000
        return [event], "t0_events_jsonl"

    async def fake_active_run(**_kwargs):
        return None

    async def fake_session_index(*_args, **_kwargs):
        return {"schema": "hive.session_index.v1", "checkpoints": [{"turn_index": 1}]}

    async def fake_runtime_tasks(*_args, **_kwargs):
        return [task]

    async def fake_goals(*_args, **_kwargs):
        return [goal]

    async def fake_teams(*_args, **_kwargs):
        return [{"id": "team-1", "member_count": 2}]

    monkeypatch.setattr(service, "_load_events", fake_load_events)
    monkeypatch.setattr(service, "get_active_web_chat_run", fake_active_run)
    monkeypatch.setattr(service, "read_session_index", fake_session_index)
    monkeypatch.setattr(service, "_list_runtime_tasks", fake_runtime_tasks)
    monkeypatch.setattr(service, "_list_goals", fake_goals)
    monkeypatch.setattr(service, "_list_teams", fake_teams)

    result = await service.build_session_workbench(object(), agent=agent, session=session)

    assert result["schema"] == "hive.ccplus.session_workbench.v1"
    assert result["session"]["id"] == str(session_id)
    assert result["turn"]["truth_source"] == "t0_events_jsonl"
    assert result["turn"]["event_count"] == 1
    assert result["turn"]["checkpoint_count"] == 1
    assert result["runtime_tasks"][0]["task_type"] == "goal_continuation"
    assert result["goals"][0]["objective"] == "Finish"
    assert result["teams"][0]["id"] == "team-1"
