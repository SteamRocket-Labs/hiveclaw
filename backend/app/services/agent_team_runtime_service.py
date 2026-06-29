"""Unified Agent Team runtime operations.

Agent Team is the CC AgentTool teammate branch expressed in Hive session
runtime terms: ``team_create`` creates a session-local team container and
``spawn_subagent(team_name + name)`` creates/starts addressable teammate child
sessions. It is deliberately separate from A2A employee delegation.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select

from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.agent import Agent
from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember
from app.models.chat_session import ChatSession
from app.models.user import User
from app.runtime.hooks import HookEvent, emit_hook
from app.services.agent_team_contract import teammate_creation_discovery
from app.services.agent_session_continuation import (
    continue_agent_session_from_mailbox,
    continue_parent_session_with_task_notification,
)
from app.services.chat_message_parts import build_session_native_event
from app.services.chat_transcript import append_session_event
from app.services.tenant_resolver import resolve_tenant_for_agent


@dataclass(frozen=True)
class TeamMemberCreateSpec:
    name: str
    role: str = ""
    model_id: uuid.UUID | str | None = None
    tool_policy: dict[str, Any] | None = None
    budget: dict[str, Any] | None = None
    prompt: str | None = None
    display_content: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentTeamRuntimeCreateResult:
    team: AgentTeam
    members: list[AgentTeamMember]
    member_sessions: list[ChatSession]
    payload: dict[str, Any]


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


def _team_member_payload(member: AgentTeamMember) -> dict[str, Any]:
    return {
        "id": str(member.id),
        "member_name": member.member_name,
        "member_role": member.member_role,
        "chat_session_id": str(member.chat_session_id),
        "runtime_task_id": str(member.runtime_task_id) if member.runtime_task_id else None,
        "runtime_task_type": member.runtime_task_type,
        "status": member.status,
    }


def _unique_member_name(requested: str, existing: list[AgentTeamMember]) -> str:
    base = str(requested or "").strip()
    if not base:
        raise ValueError("Team member name is required")
    used = {str(member.member_name or "").strip().lower() for member in existing}
    if base.lower() not in used:
        return base
    idx = 2
    while True:
        candidate = f"{base}-{idx}"
        if candidate.lower() not in used:
            return candidate
        idx += 1


def team_payload(team: AgentTeam, members: list[AgentTeamMember], *, requires_api_persist: bool = False) -> dict[str, Any]:
    return {
        "requires_api_persist": requires_api_persist,
        "id": str(team.id),
        "name": team.name,
        "status": team.status,
        "transcript_truth": team.transcript_truth,
        "lead_agent_id": str(team.lead_agent_id),
        "parent_session_id": str(team.parent_session_id),
        "members": [_team_member_payload(member) for member in members],
        **teammate_creation_discovery(team.name),
    }


async def _append_team_member_parent_event(
    *,
    db: Any,
    agent: Any,
    user: Any,
    parent_session: Any,
    team: AgentTeam,
    member: AgentTeamMember,
    source: str,
    command: str | None,
) -> None:
    payload = {
        "type": "team_member",
        "status": "created",
        "message": f"Team member created: {member.member_name}",
        "team_id": str(team.id),
        "team_name": team.name,
        "child_session_id": str(member.chat_session_id),
        "member_id": str(member.id),
        "member_name": member.member_name,
        "command": command,
        "source": source,
    }
    event = build_session_native_event(payload)
    await append_session_event(
        db=db,
        agent_id=agent.id,
        tenant_id=getattr(agent, "tenant_id", None),
        session_id=str(parent_session.id),
        actor_type="system",
        event_type="team_member",
        role="system",
        user_id=getattr(user, "id", None),
        root_session_id=str(getattr(parent_session, "root_session_id", None) or parent_session.id),
        parent_session_id=str(parent_session.id),
        content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        source="agent_team",
        parts=[event["part"]] if isinstance(event.get("part"), dict) else None,
        metadata={"source": source, **{key: value for key, value in payload.items() if value is not None}},
    )


def _build_team_member_records(
    *,
    agent: Any,
    user: Any,
    parent_session: Any,
    team: AgentTeam,
    spec: TeamMemberCreateSpec,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[AgentTeamMember, ChatSession]:
    member_name = str(spec.name or "").strip()
    if not member_name:
        raise ValueError("Team member name is required")
    parent_session_id = _uuid_or_none(getattr(parent_session, "id", None))
    if parent_session_id is None:
        raise ValueError("A valid parent session is required")

    root_session_id = _uuid_or_none(getattr(parent_session, "root_session_id", None)) or parent_session_id
    member_session_id = uuid.uuid4()
    member_role = str(spec.role or "").strip()
    member_metadata = {
        "runtime_policy": "enterable_chat_session",
        "direct_chat_supported": True,
        "source": source,
        **(metadata or {}),
        **(spec.metadata or {}),
    }
    member_session = ChatSession(
        id=member_session_id,
        agent_id=agent.id,
        tenant_id=getattr(agent, "tenant_id", None),
        user_id=getattr(user, "id", None),
        title=f"{team.name} / {member_name}"[:200],
        source_channel="agent_team",
        session_kind="team_member",
        actor_type="agent",
        runtime_source="team_member",
        visibility_scope="team",
        listed_surface="chat",
        parent_session_id=parent_session_id,
        root_session_id=root_session_id,
        transcript_metadata_json={
            "team_id": str(team.id),
            "member_name": member_name,
            "member_role": member_role,
            "source": source,
            **(metadata or {}),
            **(spec.metadata or {}),
        },
    )
    member = AgentTeamMember(
        id=uuid.uuid4(),
        team_id=team.id,
        member_name=member_name[:160],
        member_role=member_role or None,
        model_id=_uuid_or_none(spec.model_id),
        chat_session_id=member_session_id,
        tool_policy_json=spec.tool_policy if isinstance(spec.tool_policy, dict) else None,
        budget_json=spec.budget if isinstance(spec.budget, dict) else None,
        metadata_json=member_metadata,
    )
    return member, member_session


async def create_agent_team_runtime_result(
    *,
    db: Any,
    agent: Any,
    user: Any,
    parent_session: Any,
    name: str,
    members: list[TeamMemberCreateSpec],
    source: str,
    command: str | None = None,
    metadata: dict[str, Any] | None = None,
    append_parent_events: bool = True,
    emit_created_hook: bool = True,
) -> AgentTeamRuntimeCreateResult:
    team_name = str(name or "").strip()
    if not team_name:
        raise ValueError("Team name is required")

    parent_session_id = _uuid_or_none(getattr(parent_session, "id", None))
    if parent_session_id is None:
        raise ValueError("A valid parent session is required")
    if members:
        raise ValueError(
            "TeamCreate creates the Team container only; spawn teammates with spawn_subagent team_name + name"
        )

    team = AgentTeam(
        id=uuid.uuid4(),
        tenant_id=getattr(agent, "tenant_id", None),
        lead_agent_id=agent.id,
        parent_session_id=parent_session_id,
        name=team_name[:160],
        created_by_user_id=getattr(user, "id", None),
        metadata_json={
            "source": source,
            "command": command,
            "session_id": str(parent_session_id),
            "member_runtime_policy": "enterable_chat_session",
            **(metadata or {}),
        },
    )
    db.add(team)

    created_members: list[AgentTeamMember] = []
    member_sessions: list[ChatSession] = []

    db.add(
        AgentTeamEvent(
            id=uuid.uuid4(),
            team_id=team.id,
            event_type="team_created",
            payload_json={"name": team.name, "member_count": len(created_members), "source": source},
        )
    )

    if emit_created_hook:
        await emit_hook(
            HookEvent.TEAM_CREATED,
            agent_id=agent.id,
            session_id=str(parent_session_id),
            source="agent_team",
            metadata={
                "tenant_id": str(getattr(agent, "tenant_id", "") or ""),
                "team_id": str(team.id),
                "name": team.name,
                "runtime_path": "agent_team_runtime_service",
            },
        )

    await db.flush()
    if append_parent_events:
        for member in created_members:
            await _append_team_member_parent_event(
                db=db,
                agent=agent,
                user=user,
                parent_session=parent_session,
                team=team,
                member=member,
                source=source,
                command=command,
            )

    return AgentTeamRuntimeCreateResult(
        team=team,
        members=created_members,
        member_sessions=member_sessions,
        payload=team_payload(team, created_members, requires_api_persist=False),
    )


async def create_agent_team_runtime(**kwargs: Any) -> dict[str, Any]:
    result = await create_agent_team_runtime_result(**kwargs)
    return result.payload


async def spawn_agent_team_member_runtime(
    *,
    db: Any,
    agent: Any,
    user: Any,
    parent_session: Any,
    team: AgentTeam,
    spec: TeamMemberCreateSpec,
    prompt: str,
    source: str,
    mode: str = "",
) -> dict[str, Any]:
    """Create and start one teammate from the AgentTool ``team_name + name`` branch."""

    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        raise ValueError("prompt is required for teammate spawn")

    existing_members: list[AgentTeamMember] = []
    if hasattr(db, "execute"):
        existing_result = await db.execute(
            select(AgentTeamMember).where(AgentTeamMember.team_id == team.id).order_by(AgentTeamMember.created_at.asc())
        )
        existing_members = list(existing_result.scalars().all())
    unique_name = _unique_member_name(spec.name, existing_members)
    member_spec = TeamMemberCreateSpec(
        name=unique_name,
        role=spec.role,
        model_id=spec.model_id,
        tool_policy=spec.tool_policy,
        budget=spec.budget,
        prompt=spec.prompt,
        display_content=spec.display_content,
        metadata={**(spec.metadata or {}), "agent_tool_branch": "teammate_spawn", "mode": mode},
    )
    member, member_session = _build_team_member_records(
        agent=agent,
        user=user,
        parent_session=parent_session,
        team=team,
        spec=member_spec,
        source=source,
    )
    db.add(member_session)
    db.add(member)
    db.add(
        AgentTeamEvent(
            id=uuid.uuid4(),
            team_id=team.id,
            receiver_member_id=member.id,
            event_type="member_spawned",
            payload_json={
                "member_name": member.member_name,
                "member_role": member.member_role,
                "source": source,
                "mode": mode,
            },
        )
    )
    await db.flush()
    await _append_team_member_parent_event(
        db=db,
        agent=agent,
        user=user,
        parent_session=parent_session,
        team=team,
        member=member,
        source=source,
        command="spawn_subagent",
    )
    run_payload = await message_agent_team_members_runtime(
        db=db,
        agent=agent,
        user=user,
        team=team,
        members=[member],
        member_sessions=[member_session],
        message=prompt_text,
        display_content=spec.display_content or prompt_text,
        interrupt_requested=False,
        source=source,
    )
    return {
        "ok": bool(run_payload.get("ok")),
        "status": "teammate_spawned",
        "team_id": str(team.id),
        "team_name": team.name,
        "member": _team_member_payload(member),
        "member_name": member.member_name,
        "child_session_id": str(member.chat_session_id),
        "prompt": prompt_text,
        "run": run_payload,
    }


def _stamp_member_runtime(member: AgentTeamMember, run: dict[str, Any], *, status: str) -> None:
    run_id = run.get("run_id") or run.get("id")
    run_uuid = _uuid_or_none(run_id)
    if run_uuid is not None:
        member.runtime_task_id = run_uuid
    member.status = "running" if status in {"queued", "started", "running"} else status
    metadata = dict(member.metadata_json or {})
    metadata["last_runtime_status"] = status
    metadata["last_runtime_payload"] = run
    if run_id:
        metadata["last_runtime_task_id"] = str(run_id)
    member.metadata_json = metadata


def _member_terminal_status(status: str) -> str:
    value = str(status or "").strip().lower()
    if value == "completed":
        return "completed"
    if value in {"failed", "killed", "skipped", "cancelled", "canceled"}:
        return "failed"
    return value or "completed"


async def _wake_parent_session_from_team_member_completion(
    *,
    db: Any,
    member: AgentTeamMember,
    task: Any,
    status: str,
    result_summary: str | None,
    metadata: dict[str, Any],
) -> None:
    run_id = _uuid_or_none(getattr(task, "id", None))
    task_id = str(run_id or getattr(task, "id", "") or member.runtime_task_id or member.id)
    idempotency_key = f"agent_team_parent_task_notification:{member.id}:{task_id}:{status}"
    existing = metadata.get("parent_task_notification_side_effect")
    if isinstance(existing, dict) and existing.get("idempotency_key") == idempotency_key:
        return

    team = (
        await db.execute(select(AgentTeam).where(AgentTeam.id == member.team_id).limit(1))
    ).scalar_one_or_none()
    if team is None:
        return
    parent_session = (
        await db.execute(
            select(ChatSession)
            .where(
                ChatSession.id == team.parent_session_id,
                ChatSession.agent_id == team.lead_agent_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    parent_agent = (
        await db.execute(select(Agent).where(Agent.id == team.lead_agent_id).limit(1))
    ).scalar_one_or_none()
    owner_id = (
        getattr(parent_session, "user_id", None)
        or getattr(team, "created_by_user_id", None)
        or getattr(parent_agent, "creator_id", None)
    )
    owner = (
        await db.execute(select(User).where(User.id == owner_id).limit(1))
    ).scalar_one_or_none() if owner_id else None
    if parent_session is None or parent_agent is None or owner is None:
        return

    metadata["parent_task_notification_side_effect"] = {
        "idempotency_key": idempotency_key,
        "source": "agent_team",
        "task_id": task_id,
        "status": status,
    }
    member.metadata_json = metadata
    summary = result_summary or f"Team member {member.member_name} finished with status {status}."
    await continue_parent_session_with_task_notification(
        db=db,
        agent=parent_agent,
        user=owner,
        session=parent_session,
        task_id=task_id,
        task_type="team_member",
        status=status,
        summary=summary,
        child_session_id=str(member.chat_session_id),
        child_agent_name=member.member_name,
        source="agent_team",
        metadata={
            "team_id": str(team.id),
            "team_name": team.name,
            "member_id": str(member.id),
            "member_name": member.member_name,
            "runtime_task_id": task_id,
            "runtime_task_type": "team_member",
        },
    )


async def project_agent_team_member_completion(
    *,
    db: Any,
    task: Any,
    status: str,
    result_summary: str | None,
    metadata_json: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Project a terminal team-member RuntimeTask back into the team read model."""
    if str(getattr(task, "task_type", "") or "") != "team_member":
        return None

    filters = []
    run_id = _uuid_or_none(getattr(task, "id", None))
    if run_id is not None:
        filters.append(AgentTeamMember.runtime_task_id == run_id)
    child_session_id = _uuid_or_none(getattr(task, "child_session_id", None))
    if child_session_id is not None:
        filters.append(AgentTeamMember.chat_session_id == child_session_id)
    if not filters:
        return None

    member = (
        await db.execute(
            select(AgentTeamMember)
            .where(or_(*filters) if len(filters) > 1 else filters[0])
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if member is None:
        return None

    terminal_status = _member_terminal_status(status)
    metadata = dict(member.metadata_json or {})
    source_metadata = dict(metadata_json or {})
    artifacts = source_metadata.get("artifacts") or metadata.get("artifacts") or []
    artifact_paths = source_metadata.get("artifact_paths") or metadata.get("artifact_paths") or []
    t0_refs = source_metadata.get("t0_refs") or source_metadata.get("transcript_refs") or metadata.get("t0_refs") or []
    if result_summary is not None:
        metadata["summary"] = result_summary
    metadata["status"] = terminal_status
    metadata["runtime_task_id"] = str(run_id) if run_id is not None else str(getattr(task, "id", "") or "")
    metadata["artifact_paths"] = artifact_paths
    metadata["artifacts"] = artifacts
    metadata["t0_refs"] = t0_refs
    member.status = terminal_status
    member.metadata_json = metadata

    payload = {
        "status": terminal_status,
        "runtime_task_type": "team_member",
        "run_id": metadata["runtime_task_id"],
        "summary": result_summary or "",
        "artifact_paths": artifact_paths,
        "artifacts": artifacts,
        "t0_refs": t0_refs,
    }
    db.add(
        AgentTeamEvent(
            id=uuid.uuid4(),
            team_id=member.team_id,
            receiver_member_id=member.id,
            event_type=f"member_{terminal_status}",
            payload_json=payload,
        )
    )
    await _wake_parent_session_from_team_member_completion(
        db=db,
        member=member,
        task=task,
        status=terminal_status,
        result_summary=result_summary,
        metadata=metadata,
    )
    return payload


async def message_agent_team_members_runtime(
    *,
    db: Any,
    agent: Any,
    user: Any,
    team: AgentTeam,
    members: list[AgentTeamMember],
    member_sessions: list[ChatSession],
    message: str,
    display_content: str = "",
    interrupt_requested: bool = False,
    source: str = "agent_team",
) -> dict[str, Any]:
    message_text = str(message or "").strip()
    if not message_text:
        raise ValueError("message is required")
    if not members:
        raise ValueError("No matching team members")

    sessions_by_id = {str(session.id): session for session in member_sessions}
    results: list[dict[str, Any]] = []
    for member in members:
        session = sessions_by_id.get(str(member.chat_session_id))
        if session is None:
            results.append(
                {
                    "member_id": str(member.id),
                    "member_name": member.member_name,
                    "child_session_id": str(member.chat_session_id),
                    "status": "rejected",
                    "reason": "team member session not found",
                }
            )
            continue
        run = await continue_agent_session_from_mailbox(
            db=db,
            agent=agent,
            user=user,
            session=session,
            message=message_text,
            display_content=display_content,
            interrupt_requested=interrupt_requested,
            parent_session_id=team.parent_session_id,
            runtime_task_type="team_member",
        )
        status = str(run.get("status") or "queued")
        if status in {"queued", "started", "running"}:
            _stamp_member_runtime(member, run, status=status)
            event_type = "member_message_queued"
        else:
            member.status = "blocked" if status == "rejected" else member.status
            event_type = "member_message_rejected"
        db.add(
            AgentTeamEvent(
                id=uuid.uuid4(),
                team_id=team.id,
                receiver_member_id=member.id,
                event_type=event_type,
                payload_json={
                    "status": status,
                    "runtime_task_type": "team_member",
                    "run_id": run.get("run_id"),
                    "consumer": run.get("consumer"),
                    "reason": run.get("reason"),
                    "message_preview": message_text[:240],
                    "source": source,
                },
            )
        )
        results.append(
            {
                "member_id": str(member.id),
                "member_name": member.member_name,
                "child_session_id": str(member.chat_session_id),
                "status": status,
                "consumer": run.get("consumer"),
                "run_id": run.get("run_id"),
                "reason": run.get("reason"),
            }
        )

    await db.flush()
    return {
        "ok": all(item.get("status") != "rejected" for item in results),
        "team_id": str(team.id),
        "team_name": team.name,
        "message_count": len(results),
        "interrupt_requested": interrupt_requested,
        "results": results,
    }


async def create_agent_team_from_tool_request(
    request: Any,
    *,
    name: str,
    members: list[dict[str, Any]] | None = None,
    description: str = "",
    agent_type: str = "",
) -> dict[str, Any]:
    session_id = _uuid_or_none(getattr(request.context, "session_id", None))
    if session_id is None:
        raise ValueError("team_create requires the current session_id")

    async with (
        async_session() as db,
        enter_rls_bypass(db, reason=f"agent team tool runtime resolution for agent {request.context.agent_id}"),
    ):
        agent = (
            await db.execute(select(Agent).where(Agent.id == request.context.agent_id))
        ).scalar_one_or_none()
        if agent is None:
            raise ValueError("Agent not found")
        tenant_id = getattr(agent, "tenant_id", None)

    async with tenant_scoped_session(tenant_id) as db:
        user = (await db.execute(select(User).where(User.id == request.context.user_id))).scalar_one_or_none()
        if user is None:
            raise ValueError("User not found")
        parent_session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == session_id,
                    ChatSession.agent_id == request.context.agent_id,
                )
            )
        ).scalar_one_or_none()
        if parent_session is None:
            raise ValueError("Parent session not found")
        if members:
            raise ValueError(
                "team_create creates the Team container only; spawn teammates with spawn_subagent team_name + name"
            )
        return await create_agent_team_runtime(
            db=db,
            agent=agent,
            user=user,
            parent_session=parent_session,
            name=name,
            members=[],
            source="team_create_tool",
            command="team_create",
            metadata={
                "description": str(description or "").strip(),
                "lead_agent_type": str(agent_type or "").strip(),
                "team_create_semantics": "container_only",
            },
        )


async def spawn_agent_team_member_from_tool_request(
    request: Any,
    *,
    team_name: str,
    member_name: str,
    prompt: str,
    description: str = "",
    subagent_type: str = "",
    model: str = "",
    mode: str = "",
) -> dict[str, Any]:
    session_id = _uuid_or_none(getattr(request.context, "session_id", None))
    if session_id is None:
        raise ValueError("AgentTool teammate spawn requires the current session_id")
    team_name = str(team_name or "").strip()
    member_name = str(member_name or "").strip()
    if not team_name:
        raise ValueError("team_name is required for teammate spawn")
    if not member_name:
        raise ValueError("name is required for teammate spawn")

    async with (
        async_session() as db,
        enter_rls_bypass(db, reason=f"agent team teammate spawn runtime resolution for agent {request.context.agent_id}"),
    ):
        agent = (await db.execute(select(Agent).where(Agent.id == request.context.agent_id))).scalar_one_or_none()
        if agent is None:
            raise ValueError("Agent not found")
        tenant_id = getattr(agent, "tenant_id", None)

    async with tenant_scoped_session(tenant_id) as db:
        user = (await db.execute(select(User).where(User.id == request.context.user_id))).scalar_one_or_none()
        parent_session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == session_id,
                    ChatSession.agent_id == request.context.agent_id,
                )
            )
        ).scalar_one_or_none()
        team = (
            await db.execute(
                select(AgentTeam).where(
                    AgentTeam.lead_agent_id == request.context.agent_id,
                    AgentTeam.parent_session_id == session_id,
                    AgentTeam.name == team_name,
                    AgentTeam.status == "active",
                )
            )
        ).scalar_one_or_none()
        if user is None:
            raise ValueError("User not found")
        if parent_session is None:
            raise ValueError("Parent session not found")
        if team is None:
            raise ValueError(f"Team {team_name!r} not found in the current session; call team_create first")
        return await spawn_agent_team_member_runtime(
            db=db,
            agent=agent,
            user=user,
            parent_session=parent_session,
            team=team,
            spec=TeamMemberCreateSpec(
                name=member_name,
                role=str(description or "").strip(),
                metadata={
                    "agent_type": str(subagent_type or "").strip(),
                    "model": str(model or "").strip(),
                },
            ),
            prompt=prompt,
            source="agent_tool_teammate_spawn",
            mode=mode,
        )


async def send_agent_team_message_from_tool_request(request: Any) -> dict[str, Any]:
    team_id = _uuid_or_none(getattr(request, "arguments", {}).get("team_id"))
    team_name = str(getattr(request, "arguments", {}).get("team_name") or "").strip()
    member_name = str(
        getattr(request, "arguments", {}).get("member_name") or getattr(request, "arguments", {}).get("to") or ""
    ).strip()
    message = str(getattr(request, "arguments", {}).get("message") or "").strip()
    if not member_name:
        raise ValueError("member_name/to is required; use '*' to broadcast")
    if not message:
        raise ValueError("message is required")

    tenant_id = _uuid_or_none(getattr(request.context, "tenant_id", None)) or await resolve_tenant_for_agent(
        request.context.agent_id
    )
    async with tenant_scoped_session(tenant_id) as db:
        agent = (await db.execute(select(Agent).where(Agent.id == request.context.agent_id))).scalar_one_or_none()
        user = (await db.execute(select(User).where(User.id == request.context.user_id))).scalar_one_or_none()
        team_stmt = select(AgentTeam).where(AgentTeam.lead_agent_id == request.context.agent_id)
        if team_id is not None:
            team_stmt = team_stmt.where(AgentTeam.id == team_id)
        else:
            session_id = _uuid_or_none(getattr(request.context, "session_id", None))
            team_stmt = team_stmt.where(AgentTeam.status == "active")
            if session_id is not None:
                team_stmt = team_stmt.where(AgentTeam.parent_session_id == session_id)
            if team_name:
                team_stmt = team_stmt.where(AgentTeam.name == team_name)
            team_stmt = team_stmt.order_by(AgentTeam.created_at.desc()).limit(1)
        team = (await db.execute(team_stmt)).scalar_one_or_none()
        if agent is None or user is None:
            raise ValueError("Agent Team continuation principal could not be loaded")
        if team is None:
            raise ValueError("Agent Team not found for this agent")
        if team.status == "closed":
            raise ValueError("Agent Team is closed")

        member_result = await db.execute(
            select(AgentTeamMember).where(AgentTeamMember.team_id == team.id).order_by(AgentTeamMember.created_at.asc())
        )
        all_members = list(member_result.scalars().all())
        if member_name == "*":
            members = [member for member in all_members if member.status != "closed"]
        else:
            members = [
                member
                for member in all_members
                if member.status != "closed" and member.member_name.lower() == member_name.lower()
            ]
        if not members:
            raise ValueError("No matching active team members")

        session_ids = [member.chat_session_id for member in members]
        session_result = await db.execute(
            select(ChatSession).where(
                ChatSession.id.in_(session_ids),
                ChatSession.agent_id == request.context.agent_id,
            )
        )
        member_sessions = list(session_result.scalars().all())
        payload = await message_agent_team_members_runtime(
            db=db,
            agent=agent,
            user=user,
            team=team,
            members=members,
            member_sessions=member_sessions,
            message=message,
            display_content=str(getattr(request, "arguments", {}).get("display_content") or ""),
            interrupt_requested=bool(getattr(request, "arguments", {}).get("interrupt")),
            source="send_agent_session_message",
        )
        payload["member_name"] = member_name
        return payload
