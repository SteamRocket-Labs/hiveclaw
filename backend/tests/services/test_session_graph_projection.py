"""Revert-sensitive tests for the CCPlus SessionGraphV1 workbench projection.

These exercise the REAL served projection (``build_session_workbench`` —
wired to ``GET /api/agents/{id}/sessions/{id}/workbench`` at
app/api/chat_sessions.py:648). The ``session_graph`` / ``agent_session`` slots
and the TurnStateV1-derived ``active_turn`` must reflect the real parent/child
RuntimeTask + team-member rows the projection loads; if that wiring is reverted
(session_graph dropped, or active_turn no longer derived from the live run),
these assertions fail.

Patterns mirror tests/services/test_session_control_plane.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _build_session(session_id, agent_id, tenant_id, user_id, now, *, root_session_id):
    return SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        user_id=user_id,
        title="Delegation parent",
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
        parent_session_id=None,
        root_session_id=root_session_id,
        runtime_task_id=None,
        created_at=now,
        last_message_at=now,
    )


@pytest.mark.asyncio
async def test_session_graph_surfaces_real_parent_child_and_team_nodes(monkeypatch):
    import app.services.session_control_plane as service

    agent_id = uuid4()
    session_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    child_agent_id = uuid4()
    child_session_id = uuid4()
    member_session_id = uuid4()
    member_task_id = uuid4()
    now = datetime(2026, 6, 24, tzinfo=timezone.utc)

    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    session = _build_session(session_id, agent_id, tenant_id, user_id, now, root_session_id=session_id)

    # REAL delegation runtime task: focus session is the parent, a distinct
    # child session/agent is the delegated target -> a delegated_to edge + node.
    delegation_task = SimpleNamespace(
        id=uuid4(),
        task_type="delegation",
        status="running",
        parent_agent_id=agent_id,
        child_agent_id=child_agent_id,
        parent_session_id=str(session_id),
        child_session_id=str(child_session_id),
        trace_id="trace-deleg",
        created_at=now,
        started_at=now,
        completed_at=None,
        result_summary=None,
        token_usage={},
        metadata_json={},
    )
    # Same-session continuation: no child session -> a parent_child edge on focus.
    continuation_task = SimpleNamespace(
        id=uuid4(),
        task_type="goal_continuation",
        status="completed",
        parent_agent_id=agent_id,
        child_agent_id=None,
        parent_session_id=str(session_id),
        child_session_id=None,
        trace_id="trace-cont",
        created_at=now,
        started_at=now,
        completed_at=now,
        result_summary="done",
        token_usage={"total": 5},
        metadata_json={},
    )

    team_payload = {
        "id": "team-1",
        "member_count": 1,
        "members": [
            {
                "id": str(uuid4()),
                "member_name": "researcher",
                "member_role": "research",
                "chat_session_id": str(member_session_id),
                "runtime_task_id": str(member_task_id),
                "runtime_task_type": "team_member",
                "status": "idle",
            }
        ],
    }

    async def fake_load_events(db, *, agent, session, limit):
        return [], "t0_events_jsonl"

    async def fake_active_run(**_kwargs):
        return {
            "id": "run-42",
            "status": "waiting_for_tool",
            "metadata": {
                "turn_id": "turn-7",
                "terminal_reason": None,
                "active_tool_call_ids": ["call-a", "call-b"],
                "pending_user_messages": [{"content": "hold on"}],
            },
        }

    async def fake_session_index(*_args, **_kwargs):
        return {"schema": "hive.session_index.v1", "checkpoints": []}

    async def fake_runtime_tasks(*_args, **_kwargs):
        return [delegation_task, continuation_task]

    async def fake_goals(*_args, **_kwargs):
        return []

    async def fake_teams(*_args, **_kwargs):
        return [team_payload]

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

    # --- session_graph slot exists and is contract-shaped ---
    graph = result["session_graph"]
    assert graph["schema"] == "hive.ccplus.session_graph.v1"
    assert graph["root_session_id"] == str(session_id)

    node_ids = {node["session_id"] for node in graph["nodes"]}
    # Focus session + delegated child + team member are REAL nodes.
    assert str(session_id) in node_ids
    assert str(child_session_id) in node_ids
    assert str(member_session_id) in node_ids

    child_node = next(node for node in graph["nodes"] if node["session_id"] == str(child_session_id))
    assert child_node["agent_id"] == str(child_agent_id)
    assert child_node["status"] == "running"
    assert child_node["actor_type"] == "agent"

    # --- edges classify the REAL relationships by task_type ---
    relations = {(edge["relation"], edge["from_id"], edge["to_id"]) for edge in graph["edges"]}
    assert ("delegated_to", str(session_id), str(child_session_id)) in relations
    assert ("parent_child", str(session_id), str(session_id)) in relations
    assert ("team_member", str(session_id), str(member_session_id)) in relations

    team_edge = next(edge for edge in graph["edges"] if edge["relation"] == "team_member")
    assert team_edge["runtime_task_id"] == str(member_task_id)

    deleg_edge = next(edge for edge in graph["edges"] if edge["relation"] == "delegated_to")
    assert deleg_edge["task_type"] == "delegation"

    # --- active_turn reflects REAL live run status (not a default) ---
    assert result["active_turn"]["status"] == "waiting_for_tool"
    assert result["active_turn"]["runtime_task_id"] == "run-42"
    assert result["active_turn"]["turn_id"] == "turn-7"

    # --- agent_session is AgentSessionV1-derived from REAL session row + tasks ---
    agent_session = result["agent_session"]
    assert agent_session["schema"] == "hive.ccplus.agent_session.v1"
    assert agent_session["session_id"] == str(session_id)
    assert agent_session["root_session_id"] == str(session_id)
    assert agent_session["session_kind"] == "human_chat"
    assert agent_session["actor_type"] == "user"
    assert agent_session["source"] == "web"
    # runtime_task_refs are the REAL loaded task ids.
    assert str(delegation_task.id) in agent_session["runtime_task_refs"]
    assert str(continuation_task.id) in agent_session["runtime_task_refs"]
    # The nested TurnStateV1 view carries terminal_reason + active tool-call ids.
    assert agent_session["active_turn"]["status"] == "waiting_for_tool"
    assert agent_session["active_turn"]["active_tool_call_ids"] == ["call-a", "call-b"]


@pytest.mark.asyncio
async def test_session_graph_is_empty_topology_when_no_children(monkeypatch):
    """No tasks/teams -> graph still has the REAL focus node and zero edges.

    This pins that the graph is derived (focus node present, root set) rather
    than an empty default {}; reverting the wiring drops the slot entirely.
    """
    import app.services.session_control_plane as service

    agent_id = uuid4()
    session_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    root_id = uuid4()
    now = datetime(2026, 6, 24, tzinfo=timezone.utc)

    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    session = _build_session(session_id, agent_id, tenant_id, user_id, now, root_session_id=root_id)

    async def fake_load_events(db, *, agent, session, limit):
        return [], "t0_events_jsonl"

    async def fake_active_run(**_kwargs):
        return None

    async def fake_session_index(*_args, **_kwargs):
        return {"schema": "hive.session_index.v1", "checkpoints": []}

    async def fake_empty(*_args, **_kwargs):
        return []

    monkeypatch.setattr(service, "_load_events", fake_load_events)
    monkeypatch.setattr(service, "get_active_web_chat_run", fake_active_run)
    monkeypatch.setattr(service, "read_session_index", fake_session_index)
    monkeypatch.setattr(service, "_list_runtime_tasks", fake_empty)
    monkeypatch.setattr(service, "_list_goals", fake_empty)
    monkeypatch.setattr(service, "_list_teams", fake_empty)
    monkeypatch.setattr(service, "_list_pending_approvals", fake_empty)
    monkeypatch.setattr(service, "_list_branches", fake_empty)

    result = await service.build_session_workbench(object(), agent=agent, session=session)

    graph = result["session_graph"]
    assert graph["root_session_id"] == str(root_id)
    assert [node["session_id"] for node in graph["nodes"]] == [str(session_id)]
    assert graph["edges"] == []
    # Idle session -> no active turn, agent_session still derived.
    assert result["active_turn"] is None
    assert result["agent_session"]["active_turn"] is None
    assert result["agent_session"]["session_id"] == str(session_id)
