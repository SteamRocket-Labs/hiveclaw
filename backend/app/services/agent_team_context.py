"""Prompt-facing team context for subagent/workflow coordination.

This renderer surfaces Session/T0-backed coordination context. RuntimeTask and
CoordinationSignal are read/execution models used to find active work and
mailbox updates; they are not transcript truth.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent_team import AgentTeam, AgentTeamMember
from app.models.coordination import CoordinationSignal
from app.models.runtime_task import RuntimeTask
from app.services.agent_work_ledger import read_agent_work_ledger_view

_ACTIVE_STATUSES = {"pending", "running", "in_progress", "blocked", "suspended"}
_TEAM_TASK_TYPES = {"subagent", "workflow", "delegation"}
_COMPLETE_TASK_STATUSES = {"completed", "done", "skipped"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _short(value: Any, limit: int = 220) -> str:
    text = " ".join(_clean(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _id(value: Any) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    return _clean(value)


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    text = _clean(value)
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


def _task_line(task: dict[str, Any]) -> str:
    task_id = _id(task.get("id") or task.get("task_id"))[:8]
    task_type = _clean(task.get("task_type")) or "task"
    status = _clean(task.get("status")) or "unknown"
    teammate = _clean(task.get("child_agent_name") or task.get("child_agent_id") or task.get("name")) or "teammate"
    summary = _short(task.get("result_summary") or task.get("prompt") or task.get("summary"))
    suffix = f" — {summary}" if summary else ""
    return f"- {task_type} {task_id} [{status}] {teammate}{suffix}"


def _signal_line(signal: dict[str, Any]) -> str:
    signal_type = _clean(signal.get("signal_type")) or "signal"
    sender = _clean(signal.get("from_agent_id")) or "teammate"
    thread = _clean(signal.get("thread_id"))
    content = _short(signal.get("content"))
    thread_suffix = f" thread={thread[:8]}" if thread else ""
    return f"- [{signal_type}]{thread_suffix} from {sender}: {content}"


def _team_line(team: dict[str, Any]) -> str:
    name = _clean(team.get("name")) or "Team"
    status = _clean(team.get("status")) or "active"
    team_id = _id(team.get("id"))[:8]
    return f"- team {team_id} [{status}] {name}"


def _team_member_line(member: dict[str, Any]) -> str:
    name = _clean(member.get("member_name")) or "member"
    role = _short(member.get("member_role"), limit=120)
    status = _clean(member.get("status")) or "idle"
    session_id = _id(member.get("chat_session_id"))
    session_suffix = f" session={session_id}" if session_id else ""
    role_suffix = f" — {role}" if role else ""
    return f"  - {name} [{status}]{session_suffix}{role_suffix}"


def _shared_task_line(task: dict[str, Any]) -> str:
    task_id = _clean(task.get("id"))[:8]
    status = _clean(task.get("status")) or "pending"
    owner = _clean(task.get("owner"))
    title = _short(task.get("title") or task.get("content") or task.get("subject"), limit=180)
    description = _short(task.get("description"), limit=120)
    owner_suffix = f" owner={owner}" if owner else ""
    description_suffix = f" — {description}" if description else ""
    id_suffix = f" {task_id}" if task_id else ""
    return f"- task{id_suffix} [{status}]{owner_suffix}: {title}{description_suffix}"


def render_team_context_block(
    *,
    tasks: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    signals: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    teams: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    shared_tasks: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    task_limit: int = 8,
    signal_limit: int = 8,
    team_limit: int = 5,
) -> str:
    task_rows = [task for task in (tasks or []) if isinstance(task, dict)]
    signal_rows = [signal for signal in (signals or []) if isinstance(signal, dict)]
    team_rows = [team for team in (teams or []) if isinstance(team, dict)]
    shared_task_rows = [task for task in (shared_tasks or []) if isinstance(task, dict)]
    if not task_rows and not signal_rows and not team_rows and not shared_task_rows:
        return ""

    lines: list[str] = []
    if team_rows:
        lines.append("## Agent Team Workspace")
        lines.append("Current enterable Team workspaces projected from AgentTeam rows and member sessions:")
        for team in team_rows[: max(1, team_limit)]:
            lines.append(_team_line(team))
            for member in list(team.get("members") or [])[: max(1, task_limit)]:
                if isinstance(member, dict):
                    lines.append(_team_member_line(member))
        if len(team_rows) > team_limit:
            lines.append(f"- ... {len(team_rows) - team_limit} more active team workspace(s)")

    if shared_task_rows:
        if lines:
            lines.append("")
        lines.append("## Team Shared Task List")
        lines.append(
            "Open parent-session Work Ledger todos for this Team; use owner=member_name to pick the next slice:"
        )
        for task in shared_task_rows[: max(1, task_limit)]:
            lines.append(_shared_task_line(task))
        if len(shared_task_rows) > task_limit:
            lines.append(
                f"- ... {len(shared_task_rows) - task_limit} more shared task(s); call read_ledger for detail."
            )

    if task_rows:
        if lines:
            lines.append("")
        lines.append("## Team Context")
        lines.append("Current teammate/workflow state projected from Session/T0-backed runtime records:")
        for task in task_rows[: max(1, task_limit)]:
            lines.append(_task_line(task))
        if len(task_rows) > task_limit:
            lines.append(f"- ... {len(task_rows) - task_limit} more active/recent team task(s)")

    if signal_rows:
        if lines:
            lines.append("")
        lines.append("## Teammate Mailbox")
        lines.append(
            "Unread durable coordination signals; treat completion notices as next-turn context, "
            "not as a repeated status check:"
        )
        for signal in signal_rows[: max(1, signal_limit)]:
            lines.append(_signal_line(signal))
        if len(signal_rows) > signal_limit:
            lines.append(f"- ... {len(signal_rows) - signal_limit} more mailbox signal(s)")

    return "\n".join(lines).strip()


def _runtime_task_to_dict(task: RuntimeTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "child_agent_id": str(task.child_agent_id) if task.child_agent_id else "",
        "child_agent_name": task.child_agent_name,
        "prompt": task.prompt,
        "result_summary": task.result_summary,
        "parent_session_id": task.parent_session_id,
        "child_session_id": task.child_session_id,
    }


def _signal_to_dict(signal: CoordinationSignal) -> dict[str, Any]:
    return {
        "id": signal.id,
        "from_agent_id": signal.from_agent_id,
        "to_agent_id": signal.to_agent_id,
        "content": signal.content,
        "signal_type": signal.signal_type,
        "thread_id": signal.thread_id,
    }


def _team_to_dict(team: AgentTeam) -> dict[str, Any] | None:
    if not all(hasattr(team, attr) for attr in ("id", "name", "status", "parent_session_id")):
        return None
    return {
        "id": team.id,
        "name": team.name,
        "status": team.status,
        "parent_session_id": team.parent_session_id,
        "members": [],
    }


def _member_to_dict(member: AgentTeamMember) -> dict[str, Any] | None:
    if not all(hasattr(member, attr) for attr in ("team_id", "member_name", "chat_session_id")):
        return None
    return {
        "id": member.id,
        "team_id": member.team_id,
        "member_name": member.member_name,
        "member_role": getattr(member, "member_role", None),
        "chat_session_id": member.chat_session_id,
        "status": getattr(member, "status", None),
        "runtime_task_id": getattr(member, "runtime_task_id", None),
        "runtime_task_type": getattr(member, "runtime_task_type", None),
    }


async def build_prompt_facing_team_context(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | str | None,
    session_id: str | uuid.UUID | None = None,
    task_limit: int = 8,
    signal_limit: int = 8,
) -> str:
    """Build the CC-style team context/mailbox block for the current agent turn."""

    if not tenant_id:
        return ""
    session_key = _clean(session_id)
    async with tenant_scoped_session(tenant_id) as db:
        task_stmt = (
            select(RuntimeTask)
            .where(RuntimeTask.parent_agent_id == agent_id, RuntimeTask.task_type.in_(tuple(_TEAM_TASK_TYPES)))
            .order_by(RuntimeTask.created_at.desc())
            .limit(max(1, task_limit))
        )
        if session_key:
            task_stmt = task_stmt.where(
                (RuntimeTask.parent_session_id == session_key) | (RuntimeTask.child_session_id == session_key)
            )
        tasks = [
            _runtime_task_to_dict(task)
            for task in (await db.execute(task_stmt)).scalars().all()
            if _clean(task.status) in _ACTIVE_STATUSES or _clean(task.result_summary)
        ]

        signal_stmt = (
            select(CoordinationSignal)
            .where(CoordinationSignal.to_agent_id == str(agent_id))
            .order_by(CoordinationSignal.created_at.desc())
            .limit(max(1, signal_limit))
        )
        if session_key:
            signal_stmt = signal_stmt.where(CoordinationSignal.thread_id == session_key)
        signals = [_signal_to_dict(signal) for signal in (await db.execute(signal_stmt)).scalars().all()]

        team_stmt = (
            select(AgentTeam)
            .where(AgentTeam.lead_agent_id == agent_id, AgentTeam.status == "active")
            .order_by(AgentTeam.created_at.desc())
            .limit(5)
        )
        session_uuid = _uuid_or_none(session_key)
        if session_uuid is not None:
            team_stmt = team_stmt.where(AgentTeam.parent_session_id == session_uuid)
        teams = [
            item
            for item in (_team_to_dict(team) for team in (await db.execute(team_stmt)).scalars().all())
            if item is not None
        ]
        if not teams and session_uuid is not None:
            member_rows = (
                (await db.execute(select(AgentTeamMember).where(AgentTeamMember.chat_session_id == session_uuid)))
                .scalars()
                .all()
            )
            member_team_ids = [member.team_id for member in member_rows if getattr(member, "team_id", None)]
            if member_team_ids:
                teams = [
                    item
                    for item in (
                        _team_to_dict(team)
                        for team in (
                            await db.execute(
                                select(AgentTeam).where(
                                    AgentTeam.id.in_(member_team_ids),
                                    AgentTeam.lead_agent_id == agent_id,
                                    AgentTeam.status == "active",
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if item is not None
                ]
        if teams:
            team_ids = [team["id"] for team in teams]
            member_stmt = (
                select(AgentTeamMember)
                .where(AgentTeamMember.team_id.in_(team_ids))
                .order_by(AgentTeamMember.created_at.asc())
            )
            members = [
                item
                for item in (_member_to_dict(member) for member in (await db.execute(member_stmt)).scalars().all())
                if item is not None
            ]
            by_team = {str(team["id"]): team for team in teams}
            for member in members:
                team = by_team.get(str(member["team_id"]))
                if team is not None:
                    team["members"].append(member)
        shared_tasks: list[dict[str, Any]] = []
        for team in teams:
            parent_session_id = _id(team.get("parent_session_id"))
            if not parent_session_id:
                continue
            view = read_agent_work_ledger_view(agent_id=agent_id, session_id=parent_session_id)
            if not isinstance(view, dict):
                continue
            for item in view.get("todo_items") or []:
                if not isinstance(item, dict):
                    continue
                if _clean(item.get("status")).lower() in _COMPLETE_TASK_STATUSES:
                    continue
                shared_tasks.append(item)

    return render_team_context_block(
        tasks=tasks,
        signals=signals,
        teams=teams,
        shared_tasks=shared_tasks,
        task_limit=task_limit,
        signal_limit=signal_limit,
    )
