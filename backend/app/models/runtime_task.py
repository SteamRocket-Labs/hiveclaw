"""Runtime task model for persistent subagent lifecycle tracking.

Tracks agent-to-agent delegation tasks with full lifecycle:
spawn → running → completed/failed/killed.

This is separate from the business-layer Task model (models/task.py)
which tracks user-facing tasks. RuntimeTask tracks the internal
agent execution machinery.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RuntimeTask(Base):
    """Persistent record of a subagent delegation task."""

    __tablename__ = "runtime_tasks"

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

    # Lifecycle
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", index=True,
    )  # pending → running → completed | failed | killed

    # Context
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Tracing
    trace_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    parent_session_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    child_session_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    depth: Mapped[int] = mapped_column(default=1)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Metadata
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
