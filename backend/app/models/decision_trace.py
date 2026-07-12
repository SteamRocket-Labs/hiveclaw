"""Tenant-scoped decision trace records."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DecisionTraceRecord(Base):
    """Append-only decision trace row.

    JSONL remains a compatibility/debug artifact; this table is the operator
    query surface because it carries tenant/session/tool join keys and can be
    protected by RLS.
    """

    __tablename__ = "decision_traces"
    __table_args__ = (
        Index("ix_decision_traces_tenant_session", "tenant_id", "session_id"),
        Index("ix_decision_traces_tenant_agent", "tenant_id", "agent_id"),
        Index("ix_decision_traces_tool_created", "tool_name", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    decision_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    tool_name: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    checkpoint_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    chosen: Mapped[str] = mapped_column(String(80), nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    alternatives_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    situational_factors_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    charter_zone: Mapped[str] = mapped_column(String(80), nullable=False)
    preflight_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sensitivity: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class DecisionTraceFeedbackRecord(Base):
    """Append-only feedback linked to a decision trace."""

    __tablename__ = "decision_trace_feedback"
    __table_args__ = (
        Index("ix_decision_trace_feedback_tenant_decision", "tenant_id", "decision_id"),
        Index("ix_decision_trace_feedback_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    decision_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    refs: Mapped[str] = mapped_column(String(160), nullable=False)
    reaction: Mapped[str] = mapped_column(String(80), nullable=False)
    polarity: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    rationale_from_owner: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


from app.models.agent import Agent  # noqa: E402, F401
from app.models.tenant import Tenant  # noqa: E402, F401
from app.models.user import User  # noqa: E402, F401
