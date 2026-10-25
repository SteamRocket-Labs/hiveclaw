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


def _render_text(value: Any) -> str:
    return " ".join(_clean(value).split())


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
    task_id = _id(task.get("id") or task.get("task_id"))
    task_type = _clean(task.get("task_type")) or "task"
    status = _clean(task.get("status")) or "unknown"
    teammate = _clean(task.get("child_agent_name") or task.get("child_agent_id") or task.get("name")) or "teammate"
    publication = task.get("result_publication")
    summary = (_render_text(f"{publication['reason_code']}: {publication['next_action']}")
        if publication else _render_text(task.get("result_summary") or task.get("prompt") or task.get("summary")))
    suffix = f" — {summary}" if summary else ""
    return f"- {task_type} {task_id} [{status}] {teammate}{suffix}"


def _signal_line(signal: dict[str, Any]) -> str:
    signal_type = _clean(signal.get("signal_type")) or "signal"
    sender = _clean(signal.get("from_agent_id")) or "teammate"
    thread = _clean(signal.get("thread_id"))
    content = _render_text(signal.get("content"))
    thread_suffix = f" thread={thread}" if thread else ""
    return f"- [{signal_type}]{thread_suffix} from {sender}: {content}"


def _team_line(team: dict[str, Any]) -> str:
    name = _clean(team.get("name")) or "Team"
    status = _clean(team.get("status")) or "active"
    team_id = _id(team.get("id"))
    return f"- team {team_id} [{status}] {name}"


def _team_member_line(member: dict[str, Any]) -> str:
    name = _clean(member.get("member_name")) or "member"
    role = _render_text(member.get("member_role"))
    status = _clean(member.get("status")) or "idle"
    session_id = _id(member.get("chat_session_id"))
    session_suffix = f" session={session_id}" if session_id else ""
    role_suffix = f" — {role}" if role else ""
    return f"  - {name} [{status}]{session_suffix}{role_suffix}"


def _shared_task_line(task: dict[str, Any]) -> str:
    task_id = _clean(task.get("id"))
    status = _clean(task.get("status")) or "pending"
    owner = _clean(task.get("owner"))
    title = _render_text(task.get("title") or task.get("content") or task.get("subject"))
    description = _render_text(task.get("description"))
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
    # Retained for API compatibility; semantic prompt context is never pruned
    # by renderer-local counters.
    del task_limit, signal_limit, team_limit
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
        for team in team_rows:
            lines.append(_team_line(team))
            for member in list(team.get("members") or []):
                if isinstance(member, dict):
                    lines.append(_team_member_line(member))

    if shared_task_rows:
        if lines:
            lines.append("")
        lines.append("## Team Shared Task List")
        lines.append(
            "Open parent-session Work Ledger todos for this Team; use owner=member_name to pick the next slice:"
        )
        for task in shared_task_rows:
            lines.append(_shared_task_line(task))

    if task_rows:
        if lines:
            lines.append("")
        lines.append("## Team Context")
        lines.append("Current teammate/workflow state projected from Session/T0-backed runtime records:")
        for task in task_rows:
            lines.append(_task_line(task))

    if signal_rows:
        if lines:
            lines.append("")
        lines.append("## Teammate Mailbox")
        lines.append(
            "Unread durable coordination signals; treat completion notices as next-turn context, "
            "not as a repeated status check:"
        )
        for signal in signal_rows:
            lines.append(_signal_line(signal))

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
    source_refs: list[dict[str, str]] | None = None,
    source_unbound: list[str] | None = None,
    runtime_principal=None,
) -> str:
    """Build the CC-style team context/mailbox block for the current agent turn."""

    del task_limit, signal_limit
    if not tenant_id:
        return ""
    session_key = _clean(session_id)
    async with tenant_scoped_session(tenant_id) as db:
        task_stmt = (
            select(RuntimeTask)
            .where(RuntimeTask.parent_agent_id == agent_id, RuntimeTask.task_type.in_(tuple(_TEAM_TASK_TYPES)))
            .order_by(RuntimeTask.created_at.desc())
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
        )
        if session_key:
            signal_stmt = signal_stmt.where(CoordinationSignal.thread_id == session_key)
        signals = [_signal_to_dict(signal) for signal in (await db.execute(signal_stmt)).scalars().all()]

        team_stmt = (
            select(AgentTeam)
            .where(AgentTeam.lead_agent_id == agent_id, AgentTeam.status == "active")
            .order_by(AgentTeam.created_at.desc())
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

    refs, pending = [], []
    complete = bool(session_uuid) and not (signals or teams or shared_tasks)
    if session_uuid and tasks:
        from app.models.chat_session import ChatSession
        from app.services.runtime_result_sources import result_source_reference
        from app.services.runtime_task_read_sources import _pending_result_record, _task_sources
        from app.services.runtime_task_service import _task_to_dict

        async with tenant_scoped_session(tenant_id) as db:
            session = await db.get(ChatSession, session_uuid)
            for index, task in enumerate(tasks):
                if task["task_type"] != "delegation" or task["status"] != "completed":
                    complete = False
                    continue
                ref = await result_source_reference(db, tenant_id=tenant_id, agent_id=agent_id,
                    session_id=session_uuid, task_id=task["id"])
                if ref is not None:
                    refs.append(ref)
                    continue
                row = await db.get(RuntimeTask, task["id"])
                private_owner = (session is not None and row is not None and session.tenant_id == row.tenant_id
                    and session.agent_id == agent_id and session.audience_kind in {None, "private"}
                    and session.user_id == row.root_user_id and row.parent_session_id == str(session_uuid))
                sources = (await _task_sources(db, row, session_uuid)
                    if private_owner and row.task_type == "delegation" and row.status == "completed" and ref is None else None)
                if sources is not None and sources[1] is False:
                    projection = _pending_result_record(_task_to_dict(row))
                    tasks[index] = {**task, "prompt": None, "result_summary": None,
                        "result_publication": projection["result_publication"]}
                    refs.extend(sources[0])
                    pending.append(str(row.id))
                else:
                    complete = False

    rendered = render_team_context_block(
        tasks=tasks,
        signals=signals,
        teams=teams,
        shared_tasks=shared_tasks,
    )

    if rendered and source_refs is not None:
        from app.services.session_model_inputs import _hash

        if complete:
            if pending:
                from datetime import datetime, timezone
                from app.models.invocation_span import InvocationSpan
                from app.services.content_input_receipts import _runtime_input_actor, _validate_input_dependencies

                async with tenant_scoped_session(tenant_id) as db:
                    principal, user = await _runtime_input_actor(db, tenant_id, runtime_principal)
                    if (principal.agent_id != agent_id or str(principal.session_id) != str(session_uuid)
                        or user.id != session.user_id):
                        raise ValueError("pending_team_observation_scope_mismatch")
                    await _validate_input_dependencies(db, tenant_id, refs, runtime_request=runtime_principal)
                    run_id = uuid.UUID(str(principal.runtime_task_id))
                    proof = {"schema": "hive.runtime_team_publication_observation.v1", "content": rendered,
                        "sources": refs, "pending_task_ids": pending, "run_id": str(run_id),
                        "agent_id": str(agent_id), "session_id": str(session_uuid), "user_id": str(user.id)}
                    version = _hash(proof)
                    identity = uuid.uuid5(run_id, f"runtime-team-publication:{version}")
                    row = await db.get(InvocationSpan, identity)
                    if row is None:
                        db.add(InvocationSpan(id=identity, tenant_id=tenant_id, agent_id=agent_id, user_id=user.id,
                            session_id=str(session_uuid), runtime_task_id=run_id, trace_id=f"runtime-team:{run_id}",
                            span_id=f"runtime-team:{identity}", span_type="runtime_team_context",
                            name="team_publication_observed", status="captured", duration_ms=0,
                            started_at=datetime.now(timezone.utc), metadata_json={"observation": proof}))
                        await db.commit()
                    elif row.metadata_json != {"observation": proof}:
                        raise ValueError("pending_team_observation_conflict")
                    ref = {"source_kind": "runtime_team_context",
                        "source_id": f"{agent_id}\n{session_uuid}\n{identity}", "source_version": version}
            else:
                ref = {"source_kind": "runtime_team_context",
                    "source_id": f"{agent_id}\n{session_uuid}", "source_version": _hash(rendered)}
            source_refs.extend([*refs, ref])
        elif source_unbound is not None:
            source_unbound.append("runtime_team_sources_unbound")
    return rendered
