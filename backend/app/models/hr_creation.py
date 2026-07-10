"""Canonical, user-confirmed HR creation drafts."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class HrCreationDraft(Base):
    """Immutable-at-confirmation blueprint and idempotent provisioning ledger."""

    __tablename__ = "hr_creation_drafts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "creation_idempotency_key", name="uq_hr_creation_draft_idempotency"),
        CheckConstraint(
            "status IN ('awaiting_confirmation','confirmed','creating','provisioning','completed','failed','rejected','superseded','expired')",
            name="ck_hr_creation_draft_status",
        ),
        Index("ix_hr_creation_drafts_session_created", "session_id", "created_at"),
        Index("ix_hr_creation_drafts_requester_status", "requested_by_user_id", "status"),
        Index("ix_hr_creation_drafts_hr_status", "hr_agent_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    hr_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="awaiting_confirmation")
    blueprint_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    blueprint_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    blueprint_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    preview_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    creation_idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provisioning_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
