"""Persisted invocation trace spans for agent runtime observability."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class InvocationSpan(Base):
    """One persisted runtime span.

    The JSONL trace files remain a local compatibility/debug surface. This table
    is the canonical query surface for operators because it can be joined to
    RuntimeTask, user, token, and audit records by tenant/trace/request ids.
    """

    __tablename__ = "invocation_spans"
    __table_args__ = (
        UniqueConstraint("tenant_id", "trace_id", "span_id", name="uq_invocation_spans_trace_span"),
        Index("ix_invocation_spans_tenant_trace", "tenant_id", "trace_id"),
        Index("ix_invocation_spans_parent_trace", "tenant_id", "parent_trace_id"),
        Index("ix_invocation_spans_runtime_task_id", "runtime_task_id"),
        Index("ix_invocation_spans_request_id", "request_id"),
        Index("ix_invocation_spans_agent_started", "agent_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id"), nullable=True
    )
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(255), nullable=False)
    span_id: Mapped[str] = mapped_column(String(80), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    parent_trace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    span_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="ok", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    usage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# Import FK targets so isolated imports of this model keep SQLAlchemy metadata resolvable.
from app.models.agent import Agent  # noqa: E402, F401
from app.models.runtime_task import RuntimeTask  # noqa: E402, F401
from app.models.tenant import Tenant  # noqa: E402, F401
from app.models.user import User  # noqa: E402, F401
