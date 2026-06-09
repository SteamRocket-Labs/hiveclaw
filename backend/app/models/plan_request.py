"""Plan Mode request ledger — canonical source of truth for planned autonomy.

See ``docs/plan-mode-design.md`` §6.1. ``plan.md`` is the user-facing artifact,
but permissions, versioning, confirmation, concurrency, and audit all live in
this table. Nothing reaches the execution layer without a ``confirmed`` row
here whose ``plan_version`` + ``plan_hash`` were confirmed by a real user.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentPlanRequest(Base):
    """A versioned, confirmable plan governing an autonomous workflow.

    Status lifecycle (§7)::

        draft -> planning -> awaiting_confirmation -> confirmed
                          \\-> planning_failed          -> rejected
                                                        -> superseded
                                                        -> expired

    ``status`` deliberately does **not** include ``handoff_failed``: handoff
    success/failure is recorded on :attr:`handoff_status` so a failed handoff
    never corrupts the ``confirmed`` audit semantics (§13).
    """

    __tablename__ = "agent_plan_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )

    # web_chat | trigger_api | tool_runtime | objective | channel | system
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="web_chat")
    # autonomous_wake | in_session_execution | delegation
    intent_type: Mapped[str] = mapped_column(String(30), nullable=False)
    original_request: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # draft | planning | planning_failed | awaiting_confirmation
    #   | confirmed | rejected | superseded | expired
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft")

    plan_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    plan_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    plan_markdown_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    handoff_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # not_started | queued | completed | failed | skipped
    handoff_status: Mapped[str | None] = mapped_column(String(20), nullable=True)

    confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_plan_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_agent_plan_requests_agent_status", "agent_id", "status"),
        Index("ix_agent_plan_requests_tenant_status", "tenant_id", "status"),
        Index("ix_agent_plan_requests_session_created", "session_id", "created_at"),
        Index("ix_agent_plan_requests_runtime_task", "runtime_task_id"),
    )
