"""Enterable Team/member-session API for CC parity."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import authorize_session_action, check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember
from app.models.chat_session import ChatSession
from app.models.user import User
from app.runtime.hooks import HookEvent, emit_hook
from app.services.agent_team_contract import teammate_creation_discovery
from app.services.agent_team_runtime_service import (
    build_agent_team_decision_entry,
    create_agent_team_runtime,
    message_agent_team_members_runtime,
)
from app.services.chat_transcript import append_session_event
from app.services.team_runtime import TeamIndex, TeamMemberIndex, plan_team_close_consolidation
from app.services.web_chat_runtime import ActiveWebChatRunExists, start_web_chat_run

router = APIRouter(prefix="/agents/{agent_id}/agent-teams", tags=["agent-teams"])
AdminOverrideReason = Annotated[str | None, Header(alias="X-Session-Admin-Reason")]


class CreateAgentTeamIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_session_id: uuid.UUID
    name: str = Field(min_length=1)


class CreateAgentTeamEventIn(BaseModel):
    sender_member_id: uuid.UUID | None = None
    receiver_member_id: uuid.UUID | None = None
    event_type: str = Field(min_length=1)
    payload: dict = Field(default_factory=dict)


class StartAgentTeamMemberRunIn(BaseModel):
    content: str = Field(min_length=1)
    display_content: str = ""


class MessageAgentTeamMemberIn(BaseModel):
    message: str = Field(min_length=1)
    display_content: str = ""
    interrupt: bool = False


def _uuid_or_none(value: object) -> uuid.UUID | None:
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


def _member_payload(member: AgentTeamMember) -> dict:
    return {
        "id": str(member.id),
        "member_name": member.member_name,
        "member_role": member.member_role,
        "chat_session_id": str(member.chat_session_id),
        "runtime_task_id": str(member.runtime_task_id) if member.runtime_task_id else None,
        "runtime_task_type": member.runtime_task_type,
        "status": member.status,
        "tool_policy": member.tool_policy_json or {},
        "budget": member.budget_json or {},
        "metadata": member.metadata_json or {},
    }


def _team_payload(team: AgentTeam, members: list[AgentTeamMember]) -> dict:
    decision_entry = build_agent_team_decision_entry(team, list(members))
    return {
        "id": str(team.id),
        "name": team.name,
        "status": team.status,
        "transcript_truth": team.transcript_truth,
        "lead_agent_id": str(team.lead_agent_id),
        "parent_session_id": str(team.parent_session_id),
        "members": [_member_payload(member) for member in members],
        "agent_team_decision_entry": decision_entry,
        "team_outcome": decision_entry["team_outcome"],
        "lead_required_actions": decision_entry["lead_required_actions"],
        **teammate_creation_discovery(team.name),
    }


def _team_event_payload(event: AgentTeamEvent) -> dict:
    return {
        "id": str(event.id),
        "team_id": str(event.team_id),
        "sender_member_id": str(event.sender_member_id) if event.sender_member_id else None,
        "receiver_member_id": str(event.receiver_member_id) if event.receiver_member_id else None,
        "event_type": event.event_type,
        "payload": event.payload_json or {},
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def _render_team_close_summary(team: AgentTeam, *, member_outputs: list[dict], consolidation_plan: dict) -> str:
    lines = [f"Agent Team '{team.name}' 已关闭。", "", "成员输出摘要："]
    if member_outputs:
        for output in member_outputs:
            summary = str(output.get("summary") or "").strip() or "无摘要"
            lines.append(f"- {output.get('member_id')}: {summary}")
    else:
        lines.append("- 无成员输出")
    next_steps = consolidation_plan.get("next_steps") if isinstance(consolidation_plan, dict) else None
    if next_steps:
        lines.extend(["", "建议整合动作："])
        for step in next_steps:
            lines.append(f"- {step}")
    return "\n".join(lines)


async def _load_team_or_404(db: AsyncSession, *, agent_id: uuid.UUID, team_id: uuid.UUID) -> AgentTeam:
    result = await db.execute(
        select(AgentTeam).where(
            AgentTeam.id == team_id,
            AgentTeam.lead_agent_id == agent_id,
        )
    )
    team = result.scalar_one_or_none()
    if team is None:
        raise HTTPException(status_code=404, detail="Agent team not found")
    return team


async def _load_team_members(db: AsyncSession, *, team_id: uuid.UUID) -> list[AgentTeamMember]:
    result = await db.execute(
        select(AgentTeamMember).where(AgentTeamMember.team_id == team_id).order_by(AgentTeamMember.created_at.asc())
    )
    return list(result.scalars().all())


async def _load_team_member_or_404(db: AsyncSession, *, team_id: uuid.UUID, member_id: uuid.UUID) -> AgentTeamMember:
    result = await db.execute(
        select(AgentTeamMember).where(
            AgentTeamMember.id == member_id,
            AgentTeamMember.team_id == team_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=404, detail="Agent team member not found")
    return member


async def _load_member_session_or_404(db: AsyncSession, *, agent_id: uuid.UUID, member: AgentTeamMember) -> ChatSession:
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == member.chat_session_id,
            ChatSession.agent_id == agent_id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Agent team member session not found")
    return session


async def _load_team_events(db: AsyncSession, *, team_id: uuid.UUID, limit: int = 200) -> list[AgentTeamEvent]:
    result = await db.execute(
        select(AgentTeamEvent)
        .where(AgentTeamEvent.team_id == team_id)
        .order_by(AgentTeamEvent.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def _authorize_team_action(
    db: AsyncSession,
    *,
    current_user: User,
    agent_id: uuid.UUID,
    team: AgentTeam,
    action: str,
    manager_override_reason: str | None = None,
):
    return await authorize_session_action(
        db,
        current_user,
        agent_id=agent_id,
        session_id=team.parent_session_id,
        action=action,
        allow_manager_override=True,
        manager_override_reason=manager_override_reason,
    )


def _stamp_member_run(member: AgentTeamMember, *, run_id: object | None, status: str, payload: dict) -> None:
    run_uuid = _uuid_or_none(run_id)
    if run_uuid is not None:
        member.runtime_task_id = run_uuid
    member.status = "running" if status in {"running", "queued", "started"} else status
    metadata = dict(member.metadata_json or {})
    metadata["last_runtime_status"] = status
    if run_id:
        metadata["last_runtime_task_id"] = str(run_id)
    metadata["last_runtime_payload"] = payload
    member.metadata_json = metadata


def _runtime_event(team: AgentTeam, member: AgentTeamMember, *, event_type: str, payload: dict) -> AgentTeamEvent:
    return AgentTeamEvent(
        team_id=team.id,
        sender_member_id=member.id,
        event_type=event_type,
        payload_json=payload,
    )


def _team_workbench_payload(
    *,
    agent_id: uuid.UUID,
    team: AgentTeam,
    members: list[AgentTeamMember],
    events: list[AgentTeamEvent],
) -> dict:
    member_payloads = []
    for member in members:
        metadata = member.metadata_json or {}
        payload = _member_payload(member)
        payload.update(
            {
                "summary": metadata.get("summary") or "",
                "artifacts": metadata.get("artifacts") or [],
                "work_ledger_deltas": metadata.get("work_ledger_deltas") or [],
                "t0_refs": metadata.get("t0_refs") or [],
                "enter_href": f"/api/agents/{agent_id}/agent-teams/{team.id}/members/{member.id}/enter",
            }
        )
        member_payloads.append(payload)
    return {
        "schema": "hive.ccplus.agent_team_workbench.v1",
        "team": _team_payload(team, members),
        "members": member_payloads,
        "events": [_team_event_payload(event) for event in events],
        "summary": {
            "member_count": len(members),
            "active_member_count": sum(1 for member in members if member.status not in {"closed", "failed"}),
            "event_count": len(events),
            "transcript_truth": team.transcript_truth,
        },
        "links": {
            "team": f"/api/agents/{agent_id}/agent-teams/{team.id}",
            "events": f"/api/agents/{agent_id}/agent-teams/{team.id}/events",
            "close": f"/api/agents/{agent_id}/agent-teams/{team.id}/close",
        },
    }


def _ensure_member_belongs(team_id: uuid.UUID, member_id: uuid.UUID | None, members: list[AgentTeamMember]) -> None:
    if member_id is None:
        return
    if not any(member.id == member_id and member.team_id == team_id for member in members):
        raise HTTPException(status_code=400, detail="Team member does not belong to this team")


@router.post("")
async def create_agent_team(
    agent_id: uuid.UUID,
    body: CreateAgentTeamIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    authority = await authorize_session_action(
        db,
        current_user,
        agent_id=agent_id,
        session_id=body.parent_session_id,
        action="team:create",
    )
    agent = authority.agent
    parent_session = authority.session
    try:
        payload = await create_agent_team_runtime(
            db=db,
            agent=agent,
            user=current_user,
            parent_session=parent_session,
            name=body.name.strip(),
            members=[],
            source="agent_teams_api",
            metadata={"team_create_semantics": "container_only"},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload.pop("requires_api_persist", None)
    return payload


@router.get("/{team_id}/events")
async def list_agent_team_events(
    agent_id: uuid.UUID,
    team_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    admin_override_reason: AdminOverrideReason = None,
) -> list[dict]:
    await check_agent_access(db, current_user, agent_id)
    team = await _load_team_or_404(db, agent_id=agent_id, team_id=team_id)
    await _authorize_team_action(
        db,
        current_user=current_user,
        agent_id=agent_id,
        team=team,
        action="team:events:read",
        manager_override_reason=admin_override_reason,
    )
    result = await db.execute(
        select(AgentTeamEvent).where(AgentTeamEvent.team_id == team.id).order_by(AgentTeamEvent.created_at.asc())
    )
    return [_team_event_payload(event) for event in result.scalars().all()]


@router.post("/{team_id}/events")
async def create_agent_team_event(
    agent_id: uuid.UUID,
    team_id: uuid.UUID,
    body: CreateAgentTeamEventIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    admin_override_reason: AdminOverrideReason = None,
) -> dict:
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    team = await _load_team_or_404(db, agent_id=agent_id, team_id=team_id)
    await _authorize_team_action(
        db,
        current_user=current_user,
        agent_id=agent_id,
        team=team,
        action="team:event:create",
        manager_override_reason=admin_override_reason,
    )
    members = await _load_team_members(db, team_id=team.id)
    _ensure_member_belongs(team.id, body.sender_member_id, members)
    _ensure_member_belongs(team.id, body.receiver_member_id, members)
    event = AgentTeamEvent(
        team_id=team.id,
        sender_member_id=body.sender_member_id,
        receiver_member_id=body.receiver_member_id,
        event_type=body.event_type,
        payload_json=body.payload,
    )
    db.add(event)
    await db.flush()
    if body.event_type == "permission_request":
        await emit_hook(
            HookEvent.PERMISSION_REQUEST,
            agent_id=agent_id,
            session_id=str(team.parent_session_id),
            source="agent_team",
            metadata={
                "tenant_id": str(getattr(agent, "tenant_id", "") or ""),
                "team_id": str(team.id),
                "sender_member_id": str(body.sender_member_id) if body.sender_member_id else None,
                "receiver_member_id": str(body.receiver_member_id) if body.receiver_member_id else None,
                "payload": body.payload,
            },
        )
    elif body.event_type in {"idle", "blocked"}:
        await emit_hook(
            HookEvent.TEAMMATE_IDLE,
            agent_id=agent_id,
            session_id=str(team.parent_session_id),
            source="agent_team",
            metadata={
                "tenant_id": str(getattr(agent, "tenant_id", "") or ""),
                "team_id": str(team.id),
                "member_id": str(body.sender_member_id or body.receiver_member_id or ""),
                "event_type": body.event_type,
                "payload": body.payload,
            },
        )
    return _team_event_payload(event)


@router.get("")
async def list_agent_teams(
    agent_id: uuid.UUID,
    parent_session_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    admin_override_reason: AdminOverrideReason = None,
) -> list[dict]:
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    stmt = select(AgentTeam).where(AgentTeam.lead_agent_id == agent_id).order_by(AgentTeam.created_at.desc())
    if parent_session_id is not None:
        await authorize_session_action(
            db,
            current_user,
            agent_id=agent_id,
            session_id=parent_session_id,
            action="team:list",
            allow_manager_override=True,
            manager_override_reason=admin_override_reason,
        )
        stmt = stmt.where(AgentTeam.parent_session_id == parent_session_id)
    elif access_level == "manage" and str(admin_override_reason or "").strip():
        from app.services.audit_logger import write_audit_log

        await write_audit_log(
            "team_list_authority_override",
            details={"reason": str(admin_override_reason).strip(), "action": "team:list"},
            agent_id=agent_id,
            user_id=current_user.id,
        )
    else:
        stmt = stmt.join(ChatSession, ChatSession.id == AgentTeam.parent_session_id).where(
            ChatSession.user_id == current_user.id
        )
    result = await db.execute(stmt)
    teams = list(result.scalars().all())
    payloads: list[dict] = []
    for team in teams:
        members = await _load_team_members(db, team_id=team.id)
        payloads.append(_team_payload(team, members))
    return payloads


@router.get("/{team_id}")
async def get_agent_team(
    agent_id: uuid.UUID,
    team_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    admin_override_reason: AdminOverrideReason = None,
) -> dict:
    await check_agent_access(db, current_user, agent_id)
    team = await _load_team_or_404(db, agent_id=agent_id, team_id=team_id)
    await _authorize_team_action(
        db,
        current_user=current_user,
        agent_id=agent_id,
        team=team,
        action="team:read",
        manager_override_reason=admin_override_reason,
    )
    members = await _load_team_members(db, team_id=team.id)
    return _team_payload(team, members)


@router.get("/{team_id}/workbench")
async def get_agent_team_workbench(
    agent_id: uuid.UUID,
    team_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    admin_override_reason: AdminOverrideReason = None,
) -> dict:
    await check_agent_access(db, current_user, agent_id)
    team = await _load_team_or_404(db, agent_id=agent_id, team_id=team_id)
    await _authorize_team_action(
        db,
        current_user=current_user,
        agent_id=agent_id,
        team=team,
        action="team:workbench:read",
        manager_override_reason=admin_override_reason,
    )
    members = await _load_team_members(db, team_id=team.id)
    events = await _load_team_events(db, team_id=team.id)
    return _team_workbench_payload(agent_id=agent_id, team=team, members=members, events=events)


@router.get("/{team_id}/members/{member_id}/enter")
async def enter_agent_team_member(
    agent_id: uuid.UUID,
    team_id: uuid.UUID,
    member_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    admin_override_reason: AdminOverrideReason = None,
) -> dict:
    await check_agent_access(db, current_user, agent_id)
    team = await _load_team_or_404(db, agent_id=agent_id, team_id=team_id)
    await _authorize_team_action(
        db,
        current_user=current_user,
        agent_id=agent_id,
        team=team,
        action="team:member:enter",
        manager_override_reason=admin_override_reason,
    )
    result = await db.execute(
        select(AgentTeamMember).where(
            AgentTeamMember.id == member_id,
            AgentTeamMember.team_id == team.id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=404, detail="Agent team member not found")
    return {
        "team_id": str(team.id),
        "member_id": str(member.id),
        "chat_session_id": str(member.chat_session_id),
        "runtime_task_id": str(member.runtime_task_id) if member.runtime_task_id else None,
        "runtime_task_type": member.runtime_task_type,
        "status": member.status,
    }


@router.post("/{team_id}/members/{member_id}/runs")
async def start_agent_team_member_run(
    agent_id: uuid.UUID,
    team_id: uuid.UUID,
    member_id: uuid.UUID,
    body: StartAgentTeamMemberRunIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    admin_override_reason: AdminOverrideReason = None,
) -> dict:
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    team = await _load_team_or_404(db, agent_id=agent_id, team_id=team_id)
    await _authorize_team_action(
        db,
        current_user=current_user,
        agent_id=agent_id,
        team=team,
        action="team:member:run",
        manager_override_reason=admin_override_reason,
    )
    if team.status == "closed":
        raise HTTPException(status_code=409, detail="Agent team is closed")
    member = await _load_team_member_or_404(db, team_id=team.id, member_id=member_id)
    if member.status == "closed":
        raise HTTPException(status_code=409, detail="Agent team member is closed")
    session = await _load_member_session_or_404(db, agent_id=agent_id, member=member)

    metadata = {
        "source": "agent_team",
        "team_id": str(team.id),
        "member_id": str(member.id),
        "member_name": member.member_name,
        "member_role": member.member_role,
        "parent_session_id": str(team.parent_session_id),
    }
    try:
        run = await start_web_chat_run(
            db=db,
            agent=agent,
            user=current_user,
            session=session,
            content=body.content,
            display_content=body.display_content,
            runtime_task_type="team_member",
            extra_metadata=metadata,
        )
        status_value = str(run.get("status") or "running")
        event_type = "member_run_started"
    except ActiveWebChatRunExists as exc:
        run = {"status": "queued", **exc.run}
        status_value = "queued"
        event_type = "member_message_queued"

    _stamp_member_run(member, run_id=run.get("run_id"), status=status_value, payload=run)
    db.add(
        _runtime_event(
            team,
            member,
            event_type=event_type,
            payload={
                "status": status_value,
                "runtime_task_type": "team_member",
                "run_id": run.get("run_id"),
                "content_preview": body.content[:240],
            },
        )
    )
    await db.flush()
    return {**_member_payload(member), "status": status_value, "run": run}


@router.post("/{team_id}/members/{member_id}/messages")
async def message_agent_team_member(
    agent_id: uuid.UUID,
    team_id: uuid.UUID,
    member_id: uuid.UUID,
    body: MessageAgentTeamMemberIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    admin_override_reason: AdminOverrideReason = None,
) -> dict:
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    team = await _load_team_or_404(db, agent_id=agent_id, team_id=team_id)
    await _authorize_team_action(
        db,
        current_user=current_user,
        agent_id=agent_id,
        team=team,
        action="team:member:message",
        manager_override_reason=admin_override_reason,
    )
    member = await _load_team_member_or_404(db, team_id=team.id, member_id=member_id)
    session = await _load_member_session_or_404(db, agent_id=agent_id, member=member)

    payload = await message_agent_team_members_runtime(
        db=db,
        agent=agent,
        user=current_user,
        team=team,
        members=[member],
        member_sessions=[session],
        message=body.message,
        display_content=body.display_content,
        interrupt_requested=body.interrupt,
        source="agent_teams_api",
    )
    result = payload["results"][0] if payload.get("results") else {}
    return {**_member_payload(member), **result, "team_message": payload}


@router.post("/{team_id}/close")
async def close_agent_team(
    agent_id: uuid.UUID,
    team_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    admin_override_reason: AdminOverrideReason = None,
) -> dict:
    await check_agent_access(db, current_user, agent_id)
    team = await _load_team_or_404(db, agent_id=agent_id, team_id=team_id)
    await _authorize_team_action(
        db,
        current_user=current_user,
        agent_id=agent_id,
        team=team,
        action="team:close",
        manager_override_reason=admin_override_reason,
    )
    members = await _load_team_members(db, team_id=team.id)
    now = datetime.now(timezone.utc)
    team.status = "closed"
    team.closed_at = now
    for member in members:
        member.status = "closed"
        member.closed_at = now
    close_summary_ref = f"agent_team_close:{team.id}"
    team_metadata = dict(team.metadata_json or {})
    team_metadata["close_summary_ref"] = close_summary_ref
    decision_entry = build_agent_team_decision_entry(team, members, close_summary_ref=close_summary_ref)
    team_metadata["agent_team_decision_entry"] = decision_entry
    team.metadata_json = team_metadata
    db.add(
        AgentTeamEvent(
            team_id=team.id,
            event_type="team_closed",
            payload_json={
                "closed_at": now.isoformat(),
                "agent_team_decision_entry": decision_entry,
            },
        )
    )
    await emit_hook(
        HookEvent.TEAM_CLOSED,
        agent_id=agent_id,
        session_id=str(team.parent_session_id),
        source="agent_team",
        metadata={"tenant_id": str(team.tenant_id) if team.tenant_id else None, "team_id": str(team.id)},
    )

    team_index = TeamIndex(
        id=team.id,
        tenant_id=team.tenant_id,
        lead_agent_id=team.lead_agent_id,
        parent_session_id=team.parent_session_id,
        name=team.name,
        status="closed",
        transcript_truth=team.transcript_truth,
        members=[
            TeamMemberIndex(
                id=member.id,
                member_name=member.member_name,
                member_role=member.member_role or "",
                model_id=str(member.model_id) if member.model_id else None,
                chat_session_id=member.chat_session_id,
                runtime_task_id=member.runtime_task_id,
                runtime_task_type=member.runtime_task_type,
                status="closed",
                tool_policy=member.tool_policy_json or {},
                budget=member.budget_json or {},
            )
            for member in members
        ],
    )
    member_outputs = [
        {
            "member_id": str(member.id),
            "summary": (member.metadata_json or {}).get("summary") or "",
            "artifacts": (member.metadata_json or {}).get("artifacts") or [],
            "work_ledger_deltas": (member.metadata_json or {}).get("work_ledger_deltas") or [],
            "t0_refs": (member.metadata_json or {}).get("t0_refs") or [],
        }
        for member in members
    ]
    consolidation_plan = plan_team_close_consolidation(team_index, member_outputs=member_outputs)

    await append_session_event(
        db=db,
        agent_id=agent_id,
        tenant_id=team.tenant_id,
        session_id=team.parent_session_id,
        actor_type="assistant",
        event_type="assistant_message",
        role="assistant",
        user_id=current_user.id,
        content=_render_team_close_summary(team, member_outputs=member_outputs, consolidation_plan=consolidation_plan),
        source="agent_team_close",
        visibility_scope="direct_user",
        listed_surface="chat",
        metadata={
            "source": "agent_team_close",
            "team_id": str(team.id),
            "team_name": team.name,
            "member_outputs": member_outputs,
            "consolidation_plan": consolidation_plan,
            "agent_team_decision_entry": decision_entry,
        },
    )
    await db.flush()
    return {
        **_team_payload(team, members),
        "consolidation_plan": consolidation_plan,
    }
