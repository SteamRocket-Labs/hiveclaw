"""A2A collaboration group governance models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentCollaborationGroup(Base):
    """Explicit cross-owner collaboration container for A2A edges."""

    __tablename__ = "agent_collaboration_groups"
    __table_args__ = (Index("ix_agent_collaboration_groups_tenant_status", "tenant_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active", index=True
    )
    visibility: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="group_members",
        server_default="group_members",
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("status", "active")
        kwargs.setdefault("visibility", "group_members")
        kwargs.setdefault("purpose", "")
        super().__init__(**kwargs)


class AgentCollaborationGroupMember(Base):
    """Agent membership in an A2A collaboration group."""

    __tablename__ = "agent_collaboration_group_members"
    __table_args__ = (
        UniqueConstraint("group_id", "agent_id", name="uq_agent_collaboration_group_member_agent"),
        Index("ix_agent_collaboration_members_agent_status", "agent_id", "status"),
        Index("ix_agent_collaboration_members_owner_status", "agent_owner_user_id", "status"),
        Index("ix_agent_collaboration_members_tenant_group", "tenant_id", "group_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_collaboration_groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    agent_owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="member", server_default="member")
    status: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="pending_owner_confirmation",
        server_default="pending_owner_confirmation",
        index=True,
    )
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    invited_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id"), index=True
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), index=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    capability_scope: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    invitation_reason: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("status", "pending_owner_confirmation")
        kwargs.setdefault("role", "member")
        kwargs.setdefault("capability_scope", {})
        kwargs.setdefault("invitation_reason", "")
        super().__init__(**kwargs)
