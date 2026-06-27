from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_context_window_payload_extracts_latest_status_and_skipped_reason():
    import app.services.session_control_plane as service

    events = [
        SimpleNamespace(
            id=uuid4(),
            event_id=uuid4(),
            sequence=10,
            event_type="context_window_status",
            actor_type="system",
            role="system",
            content="",
            metadata_json={
                "active_context_tokens": 50,
                "auto_compact_scope_limit": 223000,
                "tokens_until_compaction": 222950,
                "cumulative_run_tokens": 1200000,
            },
            created_at=datetime(2026, 6, 24, tzinfo=timezone.utc),
        ),
        SimpleNamespace(
            id=uuid4(),
            event_id=uuid4(),
            sequence=11,
            event_type="compaction_skipped",
            actor_type="system",
            role="system",
            content="",
            metadata_json={
                "reason": "below_autocompact_threshold",
                "active_context_tokens": 50,
                "cumulative_run_tokens": 1200000,
            },
            created_at=datetime(2026, 6, 24, tzinfo=timezone.utc),
        ),
    ]

    payload = service._context_window_payload(events)

    assert payload["schema"] == "hive.ccplus.context_window.v1"
    assert payload["latest_status"]["active_context_tokens"] == 50
    assert payload["latest_skipped"]["reason"] == "below_autocompact_threshold"
    assert payload["latest_skipped"]["cumulative_run_tokens"] == 1200000
    assert payload["decision_count"] == 2


def test_compaction_payloads_ignore_context_window_decision_events():
    import app.services.session_control_plane as service

    events = [
        SimpleNamespace(
            id=uuid4(),
            event_id=uuid4(),
            sequence=10,
            event_type="compaction_skipped",
            actor_type="system",
            role="system",
            content="",
            metadata_json={"reason": "below_autocompact_threshold"},
            created_at=datetime(2026, 6, 24, tzinfo=timezone.utc),
        ),
        SimpleNamespace(
            id=uuid4(),
            event_id=uuid4(),
            sequence=11,
            event_type="session_compact",
            actor_type="system",
            role="system",
            content="summary",
            metadata_json={"kind": "compaction"},
            created_at=datetime(2026, 6, 24, tzinfo=timezone.utc),
        ),
    ]

    payloads = service._compaction_payloads(events)

    assert len(payloads) == 1
    assert payloads[0]["event_type"] == "session_compact"


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
        return {
            "id": "run-1",
            "status": "running",
            "metadata": {
                "turn_id": "turn-1",
                "pending_user_messages": [{"content": "steer"}],
                "permission_profile": {
                    "mode": "default",
                    "approval_policy": "granular",
                    "sandbox": "workspace_write",
                    "default_decision": "escalate",
                },
                "context_policy": {
                    "schema": "hive.ccplus.context_policy.v1",
                    "model_window": 128000,
                    "tool_result_inline_limit": 50000,
                },
            },
        }

    async def fake_session_index(*_args, **_kwargs):
        return {"schema": "hive.session_index.v1", "checkpoints": [{"turn_index": 1}]}

    async def fake_runtime_tasks(*_args, **_kwargs):
        return [task]

    async def fake_goals(*_args, **_kwargs):
        return [goal]

    async def fake_teams(*_args, **_kwargs):
        return [{"id": "team-1", "member_count": 2}]

    async def fake_approvals(*_args, **_kwargs):
        return []

    async def fake_branches(*_args, **_kwargs):
        return []

    monkeypatch.setattr(service, "_load_events", fake_load_events)
    monkeypatch.setattr(service, "get_active_web_chat_run", fake_active_run)
    monkeypatch.setattr(service, "read_session_index", fake_session_index)
    monkeypatch.setattr(service, "_list_runtime_tasks", fake_runtime_tasks)
    monkeypatch.setattr(service, "_list_goals", fake_goals)
    monkeypatch.setattr(service, "_list_teams", fake_teams)
    monkeypatch.setattr(service, "_list_pending_approvals", fake_approvals)
    monkeypatch.setattr(service, "_list_branches", fake_branches)

    result = await service.build_session_workbench(object(), agent=agent, session=session)

    assert result["schema"] == "hive.ccplus.session_workbench.v1"
    assert result["session"]["id"] == str(session_id)
    assert result["active_turn"] == {
        "session_id": str(session_id),
        "runtime_task_id": "run-1",
        "turn_id": "turn-1",
        "status": "running",
        "expected_turn_id": "turn-1",
        "pending_steer_count": 1,
    }
    assert result["timeline"]["truth_source"] == "t0_events_jsonl"
    assert result["timeline"]["events"][0]["content"] == "Ship it"
    assert result["turn"]["truth_source"] == "t0_events_jsonl"
    assert result["turn"]["event_count"] == 1
    assert result["turn"]["checkpoint_count"] == 1
    assert result["tool_calls"] == []
    assert result["approvals"] == []
    assert result["hooks"] == []
    assert result["compactions"] == []
    assert result["branches"] == []
    assert result["permission_profile"]["default_decision"] == "escalate"
    assert result["context_policy"]["model_window"] == 128000
    assert result["controls"]["can_start_turn"] is False
    assert result["controls"]["can_stop_active_run"] is True
    assert result["controls"]["expected_turn_id"] == "turn-1"
    assert result["runtime_tasks"][0]["task_type"] == "goal_continuation"
    assert result["goals"][0]["objective"] == "Finish"
    assert result["teams"][0]["id"] == "team-1"
