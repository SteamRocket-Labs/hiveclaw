"""Durable exactly-once handoff from terminal child work to its consumer."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RuntimeNotificationOutbox(Base):
    """One retryable completion delivery intent.

    RuntimeTask/transcript rows remain execution truth. This table is only the
    durable bridge that guarantees their terminal result reaches the parent
    mailbox or the requested user-facing session projection.
    """

    __tablename__ = "runtime_notification_outbox"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('subagent','agent_team','workflow','trigger','delegation','a2a_delegation','runtime_budget','approval')",
            name="ck_runtime_notification_outbox_source_kind",
        ),
        CheckConstraint(
            "delivery_mode IN ('parent_continuation','session_projection')",
            name="ck_runtime_notification_outbox_delivery_mode",
        ),
        CheckConstraint(
            "status IN ('pending','processing','delivered','dead_letter')",
            name="ck_runtime_notification_outbox_status",
        ),
        UniqueConstraint(
            "tenant_id",
            "source_kind",
            "source_run_id",
            "parent_session_id",
            "terminal_status",
            name="uq_runtime_notification_outbox_delivery",
        ),
        UniqueConstraint(
            "tenant_id",
            "parent_session_id",
            "mailbox_sequence",
            name="uq_runtime_notification_outbox_mailbox_sequence",
        ),
        Index("ix_runtime_notification_outbox_claim", "status", "available_at", "locked_at"),
        Index("ix_runtime_notification_outbox_page", "integration_page_id", "mailbox_sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_run_id: Mapped[str] = mapped_column(String(200), nullable=False)
    parent_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    child_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True
    )
    child_agent_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    terminal_status: Mapped[str] = mapped_column(String(40), nullable=False)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    root_runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    result_object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_result_objects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    result_ref: Mapped[str] = mapped_column(String(180), nullable=False)
    result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    artifact_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    mailbox_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    delivery_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="parent_continuation", server_default=text("'parent_continuation'")
    )
    # Ref-only routing facts. Complete summary/artifacts/model context live only
    # in RuntimeResultObject and are loaded through the governed reader.
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    payload_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default=text("100"))

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default=text("'pending'"), index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    integration_page_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_result_integration_pages.id", ondelete="SET NULL"), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_receipt_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
