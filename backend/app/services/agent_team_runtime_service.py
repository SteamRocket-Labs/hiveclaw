"""Unified Agent Team runtime operations.

Agent Team is a session-local, enterable workspace: one durable team row,
one child ChatSession per member, and team events/index rows that point back
to the parent session. It is deliberately separate from A2A employee
delegation and from lightweight session workers spawned with ``spawn_subagent``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.agent import Agent
from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember
from app.models.chat_session import ChatSession
from app.models.user import User
from app.runtime.hooks import HookEvent, emit_hook
from app.services.agent_session_continuation import continue_agent_session_from_mailbox
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
    if not members:
        raise ValueError("At least one team member is required")

    parent_session_id = _uuid_or_none(getattr(parent_session, "id", None))
    if parent_session_id is None:
        raise ValueError("A valid parent session is required")

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
    root_session_id = _uuid_or_none(getattr(parent_session, "root_session_id", None)) or parent_session_id
    for spec in members:
        member_name = str(spec.name or "").strip()
        if not member_name:
            raise ValueError("Team member name is required")
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
        db.add(member_session)
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
        db.add(member)
        created_members.append(member)
        member_sessions.append(member_session)

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


def team_member_specs_from_raw(raw_members: Any) -> list[TeamMemberCreateSpec]:
    if not isinstance(raw_members, list):
        return []
    specs: list[TeamMemberCreateSpec] = []
    for raw in raw_members:
        if not isinstance(raw, dict):
            raise ValueError("Team members must be objects")
        name = str(raw.get("name") or raw.get("member_name") or "").strip()
        if not name:
            raise ValueError("Team member name is required")
        specs.append(
            TeamMemberCreateSpec(
                name=name,
                role=str(raw.get("role") or raw.get("member_role") or "").strip(),
                model_id=raw.get("model_id"),
                tool_policy=raw.get("tool_policy") if isinstance(raw.get("tool_policy"), dict) else None,
                budget=raw.get("budget") if isinstance(raw.get("budget"), dict) else None,
                prompt=str(raw.get("prompt") or "").strip() or None,
                display_content=str(raw.get("display_content") or "").strip() or None,
                metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else None,
            )
        )
    return specs


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


async def create_agent_team_from_tool_request(request: Any, *, name: str, members: list[dict[str, Any]]) -> dict[str, Any]:
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
        return await create_agent_team_runtime(
            db=db,
            agent=agent,
            user=user,
            parent_session=parent_session,
            name=name,
            members=team_member_specs_from_raw(members),
            source="team_create_tool",
            command="team_create",
        )


async def send_agent_team_message_from_tool_request(request: Any) -> dict[str, Any]:
    team_id = _uuid_or_none(getattr(request, "arguments", {}).get("team_id"))
    member_name = str(getattr(request, "arguments", {}).get("member_name") or "").strip()
    message = str(getattr(request, "arguments", {}).get("message") or "").strip()
    if team_id is None:
        raise ValueError("team_id must be a valid UUID")
    if not member_name:
        raise ValueError("member_name is required; use '*' to broadcast")
    if not message:
        raise ValueError("message is required")

    tenant_id = _uuid_or_none(getattr(request.context, "tenant_id", None)) or await resolve_tenant_for_agent(
        request.context.agent_id
    )
    async with tenant_scoped_session(tenant_id) as db:
        agent = (await db.execute(select(Agent).where(Agent.id == request.context.agent_id))).scalar_one_or_none()
        user = (await db.execute(select(User).where(User.id == request.context.user_id))).scalar_one_or_none()
        team = (
            await db.execute(
                select(AgentTeam).where(
                    AgentTeam.id == team_id,
                    AgentTeam.lead_agent_id == request.context.agent_id,
                )
            )
        ).scalar_one_or_none()
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
