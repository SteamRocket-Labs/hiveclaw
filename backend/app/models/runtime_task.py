"""Runtime task model for persistent subagent lifecycle tracking.

Tracks agent-to-agent delegation tasks with full lifecycle:
spawn → running → completed/failed/killed.

This is separate from the business-layer Task model (models/task.py)
which tracks user-facing tasks. RuntimeTask tracks the internal
agent execution machinery.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RuntimeTask(Base):
    """Persistent record of a subagent delegation task."""

    __tablename__ = "runtime_tasks"
    __table_args__ = (
        Index(
            "uq_runtime_tasks_active_web_chat_session",
            "parent_agent_id",
            "parent_session_id",
            unique=True,
            postgresql_where=text(
                "task_type IN ('web_chat_turn', 'goal_continuation', 'team_member', 'advanced_plan') "
                "AND status IN ('pending', 'running') "
                "AND parent_agent_id IS NOT NULL "
                "AND parent_session_id IS NOT NULL"
            ),
            sqlite_where=text(
                "task_type IN ('web_chat_turn', 'goal_continuation', 'team_member', 'advanced_plan') "
                "AND status IN ('pending', 'running') "
                "AND parent_agent_id IS NOT NULL "
                "AND parent_session_id IS NOT NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    # Task type: delegation, heartbeat, trigger, coordinator_worker
    task_type: Mapped[str] = mapped_column(String(50), nullable=False, default="delegation")

    # Parent-child relationship
    parent_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True,
    )
    child_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True,
    )
    child_agent_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Tenant scope (RLS): backfilled from parent_agent_id → agents.tenant_id (nullable).
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)

    # Lifecycle
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", index=True,
    )  # pending → running → completed | failed | killed

    # Context
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Tracing
    trace_id: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    parent_session_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    child_session_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    depth: Mapped[int] = mapped_column(default=1)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True,
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Cross-process queue claim / lease metadata
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"), index=True)
    claimed_by: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))

    # Runtime budget admission metadata
    budget_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_budget_runs.id"), nullable=True, index=True
    )
    root_runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    budget_reservation_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    budget_admission_status: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    budget_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    budget_terminal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Metadata
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
