"""Durable result objects and ref-only parent integration pages."""

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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RuntimeResultObject(Base):
    """Immutable, hash-pinned complete bytes for one runtime result version."""

    __tablename__ = "runtime_result_objects"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="ck_runtime_result_objects_size_nonnegative"),
        UniqueConstraint(
            "tenant_id",
            "source_kind",
            "source_run_id",
            "sha256",
            name="uq_runtime_result_objects_source_hash",
        ),
        Index("ix_runtime_result_objects_source", "tenant_id", "source_kind", "source_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_run_id: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_schema: Mapped[str] = mapped_column(
        String(80), nullable=False, default="hive.runtime_result.v1", server_default=text("'hive.runtime_result.v1'")
    )
    payload_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(
        String(120), nullable=False, default="application/json", server_default=text("'application/json'")
    )
    encoding: Mapped[str] = mapped_column(String(32), nullable=False, default="utf-8", server_default=text("'utf-8'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RuntimeResultMailboxCursor(Base):
    """Per-parent CAS row for mailbox ordering and integration epochs."""

    __tablename__ = "runtime_result_mailbox_cursors"
    __table_args__ = (
        CheckConstraint("next_mailbox_sequence >= 1", name="ck_runtime_result_mailbox_next_sequence"),
        CheckConstraint("next_integration_epoch >= 1", name="ck_runtime_result_mailbox_next_epoch"),
        UniqueConstraint("tenant_id", "parent_session_id", name="uq_runtime_result_mailbox_cursor_parent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    next_mailbox_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    next_integration_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    last_prepared_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    last_delivered_sequence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RuntimeResultIntegrationPage(Base):
    """A durable, retryable page of ordered result refs for one parent wake."""

    __tablename__ = "runtime_result_integration_pages"
    __table_args__ = (
        CheckConstraint(
            "delivery_mode IN ('parent_continuation','session_projection')",
            name="ck_runtime_result_integration_pages_delivery_mode",
        ),
        CheckConstraint(
            "status IN ('prepared','processing','delivered','dead_letter')",
            name="ck_runtime_result_integration_pages_status",
        ),
        CheckConstraint("item_count >= 1", name="ck_runtime_result_integration_pages_item_count"),
        CheckConstraint(
            "mailbox_sequence_end >= mailbox_sequence_start",
            name="ck_runtime_result_integration_pages_sequence_range",
        ),
        UniqueConstraint(
            "tenant_id",
            "parent_session_id",
            "integration_epoch",
            name="uq_runtime_result_integration_pages_parent_epoch",
        ),
        Index(
            "ix_runtime_result_integration_pages_claim",
            "status",
            "lease_expires_at",
            "created_at",
        ),
        Index(
            "ix_runtime_result_integration_pages_root",
            "tenant_id",
            "root_scope_key",
            "integration_epoch",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    root_runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    root_scope_key: Mapped[str] = mapped_column(String(260), nullable=False)
    integration_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    delivery_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    mailbox_sequence_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mailbox_sequence_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    coverage_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="prepared", server_default=text("'prepared'"), index=True
    )
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    claimed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_receipt_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
