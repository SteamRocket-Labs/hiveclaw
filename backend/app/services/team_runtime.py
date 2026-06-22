"""Team runtime control-index primitives.

Team is an enterable session workspace under one lead session. It is not the
same as org-level A2A and it does not own transcript truth; member ChatSessions
and T0 evidence remain the transcript source of truth.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class TeamStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class TeamMemberStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    BLOCKED = "blocked"
    CLOSED = "closed"


class TeamMemberSpec(BaseModel):
    name: str
    role: str = ""
    model_id: str | None = None
    tool_policy: dict = Field(default_factory=dict)
    budget: dict = Field(default_factory=dict)


class TeamMemberIndex(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    member_name: str
    member_role: str = ""
    model_id: str | None = None
    chat_session_id: UUID = Field(default_factory=uuid4)
    runtime_task_id: UUID | None = None
    runtime_task_type: str = "team_member"
    status: TeamMemberStatus = TeamMemberStatus.IDLE
    tool_policy: dict = Field(default_factory=dict)
    budget: dict = Field(default_factory=dict)


class TeamIndex(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID | None = None
    lead_agent_id: UUID
    parent_session_id: UUID
    name: str
    status: TeamStatus = TeamStatus.ACTIVE
    transcript_truth: str = "chat_session_t0"
    members: list[TeamMemberIndex] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


def create_team_index(
    *,
    tenant_id: UUID | None,
    lead_agent_id: UUID,
    parent_session_id: UUID,
    name: str,
    members: list[TeamMemberSpec],
) -> TeamIndex:
    return TeamIndex(
        tenant_id=tenant_id,
        lead_agent_id=lead_agent_id,
        parent_session_id=parent_session_id,
        name=name,
        members=[
            TeamMemberIndex(
                member_name=member.name,
                member_role=member.role,
                model_id=member.model_id,
                tool_policy=member.tool_policy,
                budget=member.budget,
            )
            for member in members
        ],
    )


def plan_team_close_consolidation(team: TeamIndex, *, member_outputs: list[dict]) -> dict:
    summaries: list[dict] = []
    for output in member_outputs:
        summaries.append(
            {
                "member_id": str(output.get("member_id") or ""),
                "summary": str(output.get("summary") or ""),
                "artifacts": list(output.get("artifacts") or []),
                "work_ledger_deltas": list(output.get("work_ledger_deltas") or []),
                "t0_refs": list(output.get("t0_refs") or []),
            }
        )
    return {
        "team_id": str(team.id),
        "lead_agent_id": str(team.lead_agent_id),
        "parent_session_id": str(team.parent_session_id),
        "merge_mode": "summary_with_t0_refs",
        "member_summaries": summaries,
    }
