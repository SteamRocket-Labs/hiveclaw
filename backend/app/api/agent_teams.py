"""Enterable Team/member-session API for CC parity."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent_team import AgentTeam, AgentTeamMember
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.team_runtime import TeamIndex, TeamMemberIndex, plan_team_close_consolidation

router = APIRouter(prefix="/agents/{agent_id}/agent-teams", tags=["agent-teams"])


class CreateAgentTeamMemberIn(BaseModel):
    name: str = Field(min_length=1)
    role: str = ""
    model_id: uuid.UUID | None = None
    tool_policy: dict = Field(default_factory=dict)
    budget: dict = Field(default_factory=dict)


class CreateAgentTeamIn(BaseModel):
    parent_session_id: uuid.UUID
    name: str = Field(min_length=1)
    members: list[CreateAgentTeamMemberIn] = Field(min_length=1)


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
    return {
        "id": str(team.id),
        "name": team.name,
        "status": team.status,
        "transcript_truth": team.transcript_truth,
        "lead_agent_id": str(team.lead_agent_id),
        "parent_session_id": str(team.parent_session_id),
        "members": [_member_payload(member) for member in members],
    }


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


@router.post("")
async def create_agent_team(
    agent_id: uuid.UUID,
    body: CreateAgentTeamIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    team = AgentTeam(
        tenant_id=getattr(agent, "tenant_id", None),
        lead_agent_id=agent_id,
        parent_session_id=body.parent_session_id,
        name=body.name.strip(),
        created_by_user_id=current_user.id,
    )
    db.add(team)

    members_out: list[dict] = []
    for member_in in body.members:
        member_session = ChatSession(
            agent_id=agent_id,
            tenant_id=getattr(agent, "tenant_id", None),
            user_id=current_user.id,
            title=f"{body.name.strip()} / {member_in.name.strip()}",
            source_channel="agent_team",
            session_kind="team_member",
            actor_type="agent",
            runtime_source="team_member",
            visibility_scope="team",
            listed_surface="chat",
            parent_session_id=body.parent_session_id,
            root_session_id=body.parent_session_id,
        )
        db.add(member_session)
        member = AgentTeamMember(
            team_id=team.id,
            member_name=member_in.name.strip(),
            member_role=member_in.role.strip() or None,
            model_id=member_in.model_id,
            chat_session_id=member_session.id,
            tool_policy_json=member_in.tool_policy or None,
            budget_json=member_in.budget or None,
        )
        db.add(member)
        members_out.append(
            {
                "id": str(member.id),
                "member_name": member.member_name,
                "member_role": member.member_role,
                "chat_session_id": str(member.chat_session_id),
                "runtime_task_id": None,
                "runtime_task_type": member.runtime_task_type,
                "status": member.status,
            }
        )

    await db.flush()
    return {
        "id": str(team.id),
        "name": team.name,
        "status": team.status,
        "transcript_truth": team.transcript_truth,
        "lead_agent_id": str(team.lead_agent_id),
        "parent_session_id": str(team.parent_session_id),
        "members": members_out,
    }


@router.get("")
async def list_agent_teams(
    agent_id: uuid.UUID,
    parent_session_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await check_agent_access(db, current_user, agent_id)
    stmt = select(AgentTeam).where(AgentTeam.lead_agent_id == agent_id).order_by(AgentTeam.created_at.desc())
    if parent_session_id is not None:
        stmt = stmt.where(AgentTeam.parent_session_id == parent_session_id)
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
) -> dict:
    await check_agent_access(db, current_user, agent_id)
    team = await _load_team_or_404(db, agent_id=agent_id, team_id=team_id)
    members = await _load_team_members(db, team_id=team.id)
    return _team_payload(team, members)


@router.get("/{team_id}/members/{member_id}/enter")
async def enter_agent_team_member(
    agent_id: uuid.UUID,
    team_id: uuid.UUID,
    member_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await check_agent_access(db, current_user, agent_id)
    team = await _load_team_or_404(db, agent_id=agent_id, team_id=team_id)
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


@router.post("/{team_id}/close")
async def close_agent_team(
    agent_id: uuid.UUID,
    team_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await check_agent_access(db, current_user, agent_id)
    team = await _load_team_or_404(db, agent_id=agent_id, team_id=team_id)
    members = await _load_team_members(db, team_id=team.id)
    now = datetime.now(timezone.utc)
    team.status = "closed"
    team.closed_at = now
    for member in members:
        member.status = "closed"
        member.closed_at = now

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
    await db.flush()
    return {
        **_team_payload(team, members),
        "consolidation_plan": plan_team_close_consolidation(team_index, member_outputs=member_outputs),
    }
