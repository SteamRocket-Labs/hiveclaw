"""CCPlus session workbench and JSON export aggregation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.agent_session_goal import AgentSessionGoal
from app.models.agent_team import AgentTeam, AgentTeamMember
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.services.session_command_runtime import _checkpoint_payloads, _event_payload, _load_events
from app.services.session_index import read_session_index
from app.services.web_chat_runtime import get_active_web_chat_run


def _iso(value: Any) -> str | None:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value) if value else None


def _session_payload(session: ChatSession) -> dict[str, Any]:
    return {
        "id": str(session.id),
        "agent_id": str(session.agent_id),
        "tenant_id": str(session.tenant_id) if getattr(session, "tenant_id", None) else None,
        "user_id": str(session.user_id) if getattr(session, "user_id", None) else None,
        "title": session.title,
        "source_channel": session.source_channel,
        "session_kind": getattr(session, "session_kind", None) or "human_chat",
        "actor_type": getattr(session, "actor_type", None) or "user",
        "runtime_source": getattr(session, "runtime_source", None) or "web_chat",
        "visibility_scope": getattr(session, "visibility_scope", None) or "direct_user",
        "listed_surface": getattr(session, "listed_surface", None) or "chat",
        "parent_session_id": str(session.parent_session_id) if getattr(session, "parent_session_id", None) else None,
        "root_session_id": str(session.root_session_id) if getattr(session, "root_session_id", None) else None,
        "runtime_task_id": str(session.runtime_task_id) if getattr(session, "runtime_task_id", None) else None,
        "created_at": _iso(getattr(session, "created_at", None)),
        "last_message_at": _iso(getattr(session, "last_message_at", None)),
    }


def _runtime_task_payload(task: RuntimeTask) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "task_type": task.task_type,
        "status": task.status,
        "parent_agent_id": str(task.parent_agent_id) if task.parent_agent_id else None,
        "child_agent_id": str(task.child_agent_id) if task.child_agent_id else None,
        "parent_session_id": task.parent_session_id,
        "child_session_id": task.child_session_id,
        "trace_id": task.trace_id,
        "created_at": _iso(task.created_at),
        "started_at": _iso(task.started_at),
        "completed_at": _iso(task.completed_at),
        "result_summary": task.result_summary,
        "token_usage": task.token_usage or {},
        "metadata": task.metadata_json or {},
    }


def _goal_payload(goal: AgentSessionGoal) -> dict[str, Any]:
    return {
        "id": str(goal.id),
        "agent_id": str(goal.agent_id),
        "session_id": str(goal.chat_session_id),
        "objective": goal.objective,
        "status": goal.status,
        "token_budget": goal.token_budget,
        "tokens_used": goal.tokens_used,
        "time_budget_seconds": goal.time_budget_seconds,
        "continuation_count": goal.continuation_count,
        "max_continuation_turns": goal.max_continuation_turns,
        "blocked_count": goal.blocked_count,
        "completion_summary": goal.completion_summary,
        "created_at": _iso(goal.created_at),
        "updated_at": _iso(goal.updated_at),
        "completed_at": _iso(goal.completed_at),
    }


def _team_member_payload(member: AgentTeamMember) -> dict[str, Any]:
    metadata = member.metadata_json or {}
    return {
        "id": str(member.id),
        "member_name": member.member_name,
        "member_role": member.member_role,
        "chat_session_id": str(member.chat_session_id),
        "runtime_task_id": str(member.runtime_task_id) if member.runtime_task_id else None,
        "runtime_task_type": member.runtime_task_type,
        "status": member.status,
        "summary": metadata.get("summary") or "",
        "t0_refs": metadata.get("t0_refs") or [],
        "artifacts": metadata.get("artifacts") or [],
    }


def _team_payload(team: AgentTeam, members: list[AgentTeamMember]) -> dict[str, Any]:
    return {
        "id": str(team.id),
        "name": team.name,
        "status": team.status,
        "transcript_truth": team.transcript_truth,
        "lead_agent_id": str(team.lead_agent_id),
        "parent_session_id": str(team.parent_session_id),
        "member_count": len(members),
        "members": [_team_member_payload(member) for member in members],
        "created_at": _iso(team.created_at),
        "closed_at": _iso(team.closed_at),
    }


async def _list_runtime_tasks(db: AsyncSession, *, agent_id: Any, session_id: Any, limit: int = 50) -> list[RuntimeTask]:
    session_key = str(session_id)
    result = await db.execute(
        select(RuntimeTask)
        .where(
            RuntimeTask.parent_agent_id == agent_id,
            or_(
                RuntimeTask.parent_session_id == session_key,
                RuntimeTask.child_session_id == session_key,
            ),
        )
        .order_by(RuntimeTask.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def _list_goals(db: AsyncSession, *, agent_id: Any, session_id: Any) -> list[AgentSessionGoal]:
    result = await db.execute(
        select(AgentSessionGoal)
        .where(AgentSessionGoal.agent_id == agent_id, AgentSessionGoal.chat_session_id == session_id)
        .order_by(AgentSessionGoal.created_at.desc())
    )
    return list(result.scalars().all())


async def _list_teams(db: AsyncSession, *, agent_id: Any, session_id: Any) -> list[dict[str, Any]]:
    result = await db.execute(
        select(AgentTeam)
        .where(AgentTeam.lead_agent_id == agent_id, AgentTeam.parent_session_id == session_id)
        .order_by(AgentTeam.created_at.desc())
    )
    teams = list(result.scalars().all())
    payloads: list[dict[str, Any]] = []
    for team in teams:
        members_result = await db.execute(
            select(AgentTeamMember).where(AgentTeamMember.team_id == team.id).order_by(AgentTeamMember.created_at.asc())
        )
        members = list(members_result.scalars().all())
        payloads.append(_team_payload(team, members))
    return payloads


async def build_session_workbench(db: AsyncSession, *, agent: Agent, session: ChatSession) -> dict[str, Any]:
    events, truth_source = await _load_events(db, agent=agent, session=session, limit=1000)
    checkpoints = _checkpoint_payloads(events)
    latest_event = _event_payload(events[-1]) if events else None
    active_run = await get_active_web_chat_run(db=db, agent_id=agent.id, session_id=session.id)
    session_index = await read_session_index(db, agent_id=agent.id, session_id=session.id)
    runtime_tasks = await _list_runtime_tasks(db, agent_id=agent.id, session_id=session.id)
    goals = await _list_goals(db, agent_id=agent.id, session_id=session.id)
    teams = await _list_teams(db, agent_id=agent.id, session_id=session.id)
    return {
        "schema": "hive.ccplus.session_workbench.v1",
        "agent_id": str(agent.id),
        "session": _session_payload(session),
        "turn": {
            "truth_source": truth_source,
            "event_count": len(events),
            "checkpoint_count": len(checkpoints),
            "latest_event": latest_event,
            "checkpoints": checkpoints,
        },
        "controls": {
            "can_start_turn": active_run is None,
            "can_stop_active_run": active_run is not None,
            "can_export_json": True,
            "can_branch": bool(checkpoints),
            "can_start_goal": active_run is None,
            "can_create_agent_team": True,
        },
        "active_run": active_run,
        "runtime_tasks": [_runtime_task_payload(task) for task in runtime_tasks],
        "goals": [_goal_payload(goal) for goal in goals],
        "teams": teams,
        "session_index": session_index,
        "links": {
            "export": f"/api/agents/{agent.id}/sessions/{session.id}/export",
            "transcript": f"/api/agents/{agent.id}/sessions/{session.id}/transcript",
        },
    }


async def build_session_json_export(db: AsyncSession, *, agent: Agent, session: ChatSession) -> dict[str, Any]:
    workbench = await build_session_workbench(db, agent=agent, session=session)
    events, truth_source = await _load_events(db, agent=agent, session=session, limit=10000)
    return {
        "schema": "hive.ccplus.session_export.v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": str(agent.id),
        "session": workbench["session"],
        "workbench": workbench,
        "transcript": {
            "truth_source": truth_source,
            "event_count": len(events),
            "events": [_event_payload(event) for event in events],
        },
    }
