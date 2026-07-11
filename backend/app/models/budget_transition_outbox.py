"""Durable delivery intents for user-visible runtime budget transitions."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BudgetTransitionOutbox(Base):
    """Immutable address plus retryable receipts for one budget fact.

    ``RuntimeBudgetEvent`` remains the decision truth. This row only guarantees
    that one addressed transition is projected to transcript/UI and, when the
    originating session is external, back to the immutable channel target.
    """

    __tablename__ = "budget_transition_outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','processing','delivered','dead_letter','needs_reconciliation')",
            name="ck_budget_transition_outbox_status",
        ),
        UniqueConstraint("budget_event_id", name="uq_budget_transition_outbox_event"),
        Index("ix_budget_transition_outbox_claim", "status", "available_at", "locked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    budget_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_budget_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    budget_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_budget_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    transition: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    external_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("external_principals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    channel_config_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False, default="web", server_default=text("'web'"))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_target_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    delivery_receipts_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default=text("'pending'"), index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
