"""Agent Work Ledger model.

The DB row is the future canonical index for scoped runtime cognition state.
Artifacts remain useful for resume prompts and audit exports, but tenant,
agent, plan, task, and status querying belongs in the database.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentWorkLedger(Base):
    """Scoped agent execution ledger for Plan Mode and long-task runs."""

    __tablename__ = "agent_work_ledgers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_plan_requests.id", ondelete="SET NULL"), nullable=True, index=True
    )
    runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="running")
    current_phase: Mapped[str | None] = mapped_column(String(120), nullable=True)

    todo_items_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    findings_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    progress_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    failures_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    verification_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    open_questions_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    evidence_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    sensitivity_level: Mapped[str] = mapped_column(String(30), nullable=False, default="internal")

    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_updated_by: Mapped[str] = mapped_column(String(50), nullable=False, default="runtime")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_agent_work_ledgers_agent_status", "agent_id", "status"),
        Index("ix_agent_work_ledgers_tenant_status", "tenant_id", "status"),
        # plan_id and runtime_task_id already get single-column indexes from
        # index=True on their columns above; redefining them here created a
        # duplicate same-named index (DuplicateTable on a fresh create_all).
        # Exposed when RLS stage-2a made env.py import every model.
    )
