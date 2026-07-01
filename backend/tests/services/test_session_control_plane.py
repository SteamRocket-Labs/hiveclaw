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


def test_active_run_event_refs_project_skill_and_mcp_calls_into_turn_envelope():
    import app.services.session_control_plane as service

    now = datetime(2026, 6, 24, tzinfo=timezone.utc)
    events = [
        SimpleNamespace(
            id=uuid4(),
            event_id=uuid4(),
            sequence=1,
            event_type="tool_call",
            actor_type="assistant",
            role="tool_call",
            content="",
            metadata_json={"tool_name": "load_skill", "args": {"name": "Incident Response"}},
            created_at=now,
        ),
        SimpleNamespace(
            id=uuid4(),
            event_id=uuid4(),
            sequence=2,
            event_type="tool_call",
            actor_type="assistant",
            role="tool_call",
            content="",
            metadata_json={"tool_name": "call_mcp_tool", "args": {"server": "docs", "tool_name": "search"}},
            created_at=now,
        ),
    ]
    active_run = {"id": "run-1", "metadata": {"skill_catalog_refs": ["skill:existing"]}}

    enriched = service._active_run_with_event_refs(active_run, events)

    assert enriched["metadata"]["skill_catalog_refs"] == ["skill:existing", "skill:Incident Response"]
    assert enriched["metadata"]["mcp_server_refs"] == ["mcp_server:docs"]
    assert enriched["metadata"]["active_tool_names"] == ["load_skill", "call_mcp_tool"]
    assert active_run["metadata"] == {"skill_catalog_refs": ["skill:existing"]}


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
                "prompt_assembly_manifest": {
                    "schema": "hive.ccplus.prompt_assembly_manifest.v1",
                    "source_of_truth": "runtime_prompt_assembly",
                    "turn_id": "turn-1",
                    "session_id": str(session_id),
                    "context_budget": {"model_window": 128000},
                    "dynamic_sections": ["runtime_metadata_context"],
                    "actual_system_prompt_chars": 123,
                    "actual_dynamic_notice_chars": 45,
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

    async def fake_workflow_journals(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(service, "_load_events", fake_load_events)
    monkeypatch.setattr(service, "get_active_web_chat_run", fake_active_run)
    monkeypatch.setattr(service, "read_session_index", fake_session_index)
    monkeypatch.setattr(service, "_list_runtime_tasks", fake_runtime_tasks)
    monkeypatch.setattr(service, "_list_goals", fake_goals)
    monkeypatch.setattr(service, "_list_teams", fake_teams)
    monkeypatch.setattr(service, "_list_pending_approvals", fake_approvals)
    monkeypatch.setattr(service, "_list_branches", fake_branches)
    monkeypatch.setattr(service, "_list_workflow_journals", fake_workflow_journals)

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
    assert result["turn_envelope"]["schema"] == "hive.ccplus.turn_envelope.v1"
    assert result["turn_envelope"]["turn_id"] == "turn-1"
    assert result["turn_envelope"]["runtime_task_id"] == "run-1"
    assert result["turn_envelope"]["permission_profile"]["approval_policy"] == "granular"
    assert result["prompt_manifest"]["schema"] == "hive.ccplus.prompt_assembly_manifest.v1"
    assert result["prompt_manifest"]["source_of_truth"] == "runtime_prompt_assembly"
    assert result["prompt_manifest"]["turn_id"] == "turn-1"
    assert result["prompt_manifest"]["context_budget"]["model_window"] == 128000
    assert result["prompt_manifest"]["actual_system_prompt_chars"] == 123
    assert result["controls"]["can_start_turn"] is False
    assert result["controls"]["can_stop_active_run"] is True
    assert result["controls"]["expected_turn_id"] == "turn-1"
    assert result["runtime_tasks"][0]["task_type"] == "goal_continuation"
    assert result["goals"][0]["objective"] == "Finish"
    assert result["teams"][0]["id"] == "team-1"


@pytest.mark.asyncio
async def test_session_workbench_projects_background_completion_wake_state(monkeypatch):
    import app.services.session_control_plane as service

    agent_id = uuid4()
    session_id = uuid4()
    tenant_id = uuid4()
    now = datetime(2026, 6, 27, tzinfo=timezone.utc)
    team_task_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        user_id=uuid4(),
        title="Background wake review",
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
    subagent_task = SimpleNamespace(
        id=uuid4(),
        task_type="subagent",
        status="completed",
        parent_agent_id=agent_id,
        child_agent_id=None,
        parent_session_id=str(session_id),
        child_session_id=str(uuid4()),
        trace_id="trace-subagent",
        created_at=now,
        started_at=now,
        completed_at=now,
        result_summary="critic finished review",
        token_usage={"total": 42},
        metadata_json={"subagent_name": "critic", "subagent_type": "critic"},
    )
    workflow_task = SimpleNamespace(
        id=uuid4(),
        task_type="workflow",
        status="running",
        parent_agent_id=agent_id,
        child_agent_id=None,
        parent_session_id=str(session_id),
        child_session_id=str(uuid4()),
        trace_id="trace-workflow",
        created_at=now,
        started_at=now,
        completed_at=None,
        result_summary=None,
        token_usage={},
        metadata_json={"workflow_name": "Release checks"},
    )
    team_task = SimpleNamespace(
        id=team_task_id,
        task_type="team_member",
        status="completed",
        parent_agent_id=agent_id,
        child_agent_id=None,
        parent_session_id=str(session_id),
        child_session_id=str(uuid4()),
        trace_id="trace-team",
        created_at=now,
        started_at=now,
        completed_at=now,
        result_summary="team member raw runtime summary",
        token_usage={},
        metadata_json={},
    )
    foreground_turn = SimpleNamespace(
        id=uuid4(),
        task_type="web_chat_turn",
        status="completed",
        parent_agent_id=agent_id,
        child_agent_id=None,
        parent_session_id=str(session_id),
        child_session_id=None,
        trace_id="trace-turn",
        created_at=now,
        started_at=now,
        completed_at=now,
        result_summary="normal assistant turn",
        token_usage={},
        metadata_json={},
    )

    async def fake_load_events(db, *, agent, session, limit):
        return [], "t0_events_jsonl"

    async def fake_active_run(**_kwargs):
        return None

    async def fake_session_index(*_args, **_kwargs):
        return {"schema": "hive.session_index.v1", "checkpoints": []}

    async def fake_runtime_tasks(*_args, **_kwargs):
        return [subagent_task, workflow_task, team_task, foreground_turn]

    async def fake_goals(*_args, **_kwargs):
        return []

    async def fake_teams(*_args, **_kwargs):
        return [
            {
                "id": "team-1",
                "name": "Review Team",
                "status": "active",
                "members": [
                    {
                        "id": "member-1",
                        "member_name": "release critic",
                        "member_role": "Review release risk",
                        "chat_session_id": str(team_task.child_session_id),
                        "runtime_task_id": str(team_task_id),
                        "runtime_task_type": "team_member",
                        "status": "completed",
                        "summary": "team read model summary",
                        "t0_refs": ["session#event-1"],
                        "artifacts": [{"path": "workspace/release-review.md"}],
                    }
                ],
            }
        ]

    async def fake_approvals(*_args, **_kwargs):
        return []

    async def fake_branches(*_args, **_kwargs):
        return []

    async def fake_workflow_journals(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(service, "_load_events", fake_load_events)
    monkeypatch.setattr(service, "get_active_web_chat_run", fake_active_run)
    monkeypatch.setattr(service, "read_session_index", fake_session_index)
    monkeypatch.setattr(service, "_list_runtime_tasks", fake_runtime_tasks)
    monkeypatch.setattr(service, "_list_goals", fake_goals)
    monkeypatch.setattr(service, "_list_teams", fake_teams)
    monkeypatch.setattr(service, "_list_pending_approvals", fake_approvals)
    monkeypatch.setattr(service, "_list_branches", fake_branches)
    monkeypatch.setattr(service, "_list_workflow_journals", fake_workflow_journals)

    result = await service.build_session_workbench(object(), agent=agent, session=session)

    assert result["completion_wake_policy"]["schema"] == "hive.ccplus.completion_wake_policy.v1"
    assert result["completion_wake_policy"]["truth_source"] == "runtime_tasks+agent_team_read_model+session_timeline"
    assert result["completion_wake_policy"]["delivery_order"] == [
        "session_event",
        "parent_agent_wake",
        "session_workbench",
        "notification",
    ]
    assert result["completion_wake_summary"] == {
        "total": 3,
        "pending": 0,
        "running": 1,
        "completed": 2,
        "failed": 0,
        "terminal": 2,
        "needs_parent_observation": 2,
    }
    assert [wake["kind"] for wake in result["completion_wakes"]] == ["team_member", "subagent", "workflow"]
    assert [wake["runtime_task_id"] for wake in result["completion_wakes"]].count(str(team_task_id)) == 1
    team_wake = result["completion_wakes"][0]
    assert team_wake["source"] == "agent_team_read_model"
    assert team_wake["summary"] == "team read model summary"
    assert team_wake["artifacts"] == [{"path": "workspace/release-review.md"}]
    assert team_wake["t0_refs"] == ["session#event-1"]
    assert result["completion_wakes"][1]["label"] == "critic"
    assert result["completion_wakes"][2]["state"] == "running"


@pytest.mark.asyncio
async def test_runtime_sections_separate_agent_team_subagent_background_workflow(monkeypatch):
    import app.services.session_control_plane as service

    agent_id = uuid4()
    session_id = uuid4()
    tenant_id = uuid4()
    now = datetime(2026, 7, 2, tzinfo=timezone.utc)
    workflow_run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        user_id=uuid4(),
        title="Runtime taxonomy",
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
    subagent_task = SimpleNamespace(
        id=uuid4(),
        task_type="subagent",
        status="completed",
        parent_agent_id=agent_id,
        child_agent_id=None,
        parent_session_id=str(session_id),
        child_session_id=str(uuid4()),
        trace_id="trace-subagent",
        created_at=now,
        started_at=now,
        completed_at=now,
        result_summary="subagent done",
        token_usage={"total": 10},
        metadata_json={"subagent_name": "critic", "subagent_type": "critic"},
    )
    background_task = SimpleNamespace(
        id=uuid4(),
        task_type="long_task",
        status="running",
        parent_agent_id=agent_id,
        child_agent_id=None,
        parent_session_id=str(session_id),
        child_session_id=None,
        trace_id="trace-background",
        created_at=now,
        started_at=now,
        completed_at=None,
        result_summary=None,
        token_usage={},
        metadata_json={"task_name": "nightly sweep"},
    )
    workflow_task = SimpleNamespace(
        id=workflow_run_id,
        task_type="workflow",
        status="running",
        parent_agent_id=agent_id,
        child_agent_id=None,
        parent_session_id=str(session_id),
        child_session_id=None,
        trace_id="trace-workflow",
        created_at=now,
        started_at=now,
        completed_at=None,
        result_summary=None,
        token_usage={"total": 77},
        metadata_json={
            "workflow_name": "ABS deep research",
            "definition_source": "dynamic_workflow",
            "dynamic_workflow": {"proposal_id": "proposal-1"},
        },
    )
    team = {
        "id": "team-1",
        "name": "ABS Team",
        "status": "active",
        "member_count": 1,
        "members": [
            {
                "id": "member-1",
                "member_name": "CLO analyst",
                "member_role": "Analyze CLO waterfall",
                "chat_session_id": str(uuid4()),
                "runtime_task_id": str(uuid4()),
                "runtime_task_type": "team_member",
                "status": "running",
                "summary": "",
            }
        ],
    }

    async def fake_load_events(db, *, agent, session, limit):
        return [], "t0_events_jsonl"

    async def fake_active_run(**_kwargs):
        return None

    async def fake_session_index(*_args, **_kwargs):
        return {"schema": "hive.session_index.v1", "checkpoints": []}

    async def fake_runtime_tasks(*_args, **_kwargs):
        return [subagent_task, background_task, workflow_task]

    async def fake_goals(*_args, **_kwargs):
        return []

    async def fake_teams(*_args, **_kwargs):
        return [team]

    async def fake_approvals(*_args, **_kwargs):
        return []

    async def fake_branches(*_args, **_kwargs):
        return []

    async def fake_workflow_journals(*_args, **_kwargs):
        return {
            str(workflow_run_id): {
                "steps": [{"step_id": "scan", "step_type": "fanout_step", "status": "running"}],
                "leaf_calls": [
                    {
                        "step_id": "scan",
                        "leaf_id": "case-1",
                        "status": "running",
                        "child_session_id": None,
                    }
                ],
            }
        }

    monkeypatch.setattr(service, "_load_events", fake_load_events)
    monkeypatch.setattr(service, "get_active_web_chat_run", fake_active_run)
    monkeypatch.setattr(service, "read_session_index", fake_session_index)
    monkeypatch.setattr(service, "_list_runtime_tasks", fake_runtime_tasks)
    monkeypatch.setattr(service, "_list_goals", fake_goals)
    monkeypatch.setattr(service, "_list_teams", fake_teams)
    monkeypatch.setattr(service, "_list_pending_approvals", fake_approvals)
    monkeypatch.setattr(service, "_list_branches", fake_branches)
    monkeypatch.setattr(service, "_list_workflow_journals", fake_workflow_journals, raising=False)

    result = await service.build_session_workbench(object(), agent=agent, session=session)

    sections = result["runtime_sections"]
    assert list(sections.keys()) == [
        "agent_teams",
        "subagents",
        "workflows",
        "background",
        "notifications",
        "runs",
        "raw",
    ]
    assert sections["agent_teams"]["items"][0]["name"] == "ABS Team"
    assert sections["agent_teams"]["items"][0]["members"][0]["runtime_kind"] == "team_member"
    assert sections["subagents"]["items"][0]["label"] == "critic"
    assert sections["background"]["items"][0]["label"] == "nightly sweep"
    workflow = sections["workflows"]["items"][0]
    assert workflow["label"] == "ABS deep research"
    assert workflow["definition_source"] == "dynamic_workflow"
    assert workflow["steps"][0]["step_id"] == "scan"
    assert workflow["leaf_calls"][0]["leaf_id"] == "case-1"
    assert workflow["leaf_calls"][0]["enterable"] is False
    assert sections["notifications"]["items"]
    assert {row["runtime_task_id"] for row in sections["runs"]["items"]} == {
        str(subagent_task.id),
        str(background_task.id),
        str(workflow_run_id),
    }


@pytest.mark.asyncio
async def test_session_index_reports_resume_health_when_index_omits_it(monkeypatch):
    import app.services.session_control_plane as service

    agent_id = uuid4()
    session_id = uuid4()
    tenant_id = uuid4()
    now = datetime(2026, 7, 2, tzinfo=timezone.utc)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        user_id=uuid4(),
        title="Resume health",
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

    async def fake_load_events(db, *, agent, session, limit):
        return [], "t0_events_jsonl"

    async def fake_empty(*_args, **_kwargs):
        return []

    async def fake_active_run(**_kwargs):
        return None

    async def fake_session_index(*_args, **_kwargs):
        return {"schema": "hive.session_index.v1", "checkpoints": []}

    monkeypatch.setattr(service, "_load_events", fake_load_events)
    monkeypatch.setattr(service, "get_active_web_chat_run", fake_active_run)
    monkeypatch.setattr(service, "read_session_index", fake_session_index)
    monkeypatch.setattr(service, "_list_runtime_tasks", fake_empty)
    monkeypatch.setattr(service, "_list_goals", fake_empty)
    monkeypatch.setattr(service, "_list_teams", fake_empty)
    monkeypatch.setattr(service, "_list_pending_approvals", fake_empty)
    monkeypatch.setattr(service, "_list_branches", fake_empty)
    monkeypatch.setattr(service, "_list_workflow_journals", fake_empty, raising=False)

    result = await service.build_session_workbench(object(), agent=agent, session=session)

    assert result["session_index"]["resume_health"] == {
        "status": "ok",
        "reason": "no_active_run",
        "active_run_id": None,
        "checkpoint_count": 0,
    }


@pytest.mark.asyncio
async def test_pending_approvals_without_session_binding_do_not_leak_between_sessions(monkeypatch):
    import app.services.session_control_plane as service

    current_session_id = uuid4()
    other_session_id = uuid4()
    agent_id = uuid4()
    tenant_id = uuid4()
    now = datetime(2026, 7, 2, tzinfo=timezone.utc)

    class _ScalarResult:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return _ScalarResult(self._rows)

    class _DB:
        async def execute(self, *_args, **_kwargs):
            return _Result(
                [
                    SimpleNamespace(
                        id=uuid4(),
                        agent_id=agent_id,
                        tenant_id=tenant_id,
                        action_type="send_email",
                        status="pending",
                        details={"session_id": str(current_session_id), "reason": "current"},
                        created_at=now,
                        resolved_at=None,
                        resolved_by=None,
                    ),
                    SimpleNamespace(
                        id=uuid4(),
                        agent_id=agent_id,
                        tenant_id=tenant_id,
                        action_type="send_email",
                        status="pending",
                        details={"session_id": str(other_session_id), "reason": "other"},
                        created_at=now,
                        resolved_at=None,
                        resolved_by=None,
                    ),
                    SimpleNamespace(
                        id=uuid4(),
                        agent_id=agent_id,
                        tenant_id=tenant_id,
                        action_type="send_email",
                        status="pending",
                        details={"reason": "missing session"},
                        created_at=now,
                        resolved_at=None,
                        resolved_by=None,
                    ),
                ]
            )

    monkeypatch.setattr(service, "is_visible_enterprise_approval", lambda approval: True)

    approvals = await service._list_pending_approvals(
        _DB(),
        agent_id=agent_id,
        session_id=current_session_id,
        tenant_id=tenant_id,
    )

    assert [approval["details"]["reason"] for approval in approvals] == ["current"]
