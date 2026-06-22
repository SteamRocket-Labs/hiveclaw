"""Team/member session control-index models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentTeam(Base):
    __tablename__ = "agent_teams"
    __table_args__ = (
        Index("ix_agent_teams_lead_session", "lead_agent_id", "parent_session_id"),
        Index("ix_agent_teams_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    lead_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True
    )
    parent_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_sessions.id"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active", index=True
    )
    transcript_truth: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="chat_session_t0",
        server_default="chat_session_t0",
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("status", "active")
        kwargs.setdefault("transcript_truth", "chat_session_t0")
        super().__init__(**kwargs)


class AgentTeamMember(Base):
    __tablename__ = "agent_team_members"
    __table_args__ = (
        Index("ix_agent_team_members_team_status", "team_id", "status"),
        Index("ix_agent_team_members_session", "chat_session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_teams.id"), nullable=False, index=True
    )
    member_name: Mapped[str] = mapped_column(String(160), nullable=False)
    member_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("llm_models.id"), nullable=True)
    chat_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_sessions.id"),
        nullable=False,
        index=True,
    )
    runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id"), index=True
    )
    runtime_task_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="team_member",
        server_default="team_member",
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="idle", server_default="idle", index=True)
    tool_policy_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    budget_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("runtime_task_type", "team_member")
        kwargs.setdefault("status", "idle")
        super().__init__(**kwargs)


class AgentTeamEvent(Base):
    __tablename__ = "agent_team_events"
    __table_args__ = (
        Index("ix_agent_team_events_team_created", "team_id", "created_at"),
        Index("ix_agent_team_events_type", "event_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_teams.id"), nullable=False, index=True
    )
    sender_member_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_team_members.id"),
        nullable=True,
        index=True,
    )
    receiver_member_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_team_members.id"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
