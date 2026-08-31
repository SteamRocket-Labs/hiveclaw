"""Durable tenant-scoped delivery intents for committed runtime terminals."""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import (
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


class RuntimeTerminalBoundaryOutbox(Base):
    """One retryable hook boundary backed by an already-committed authority.

    The row deliberately stores no transcript or model-authored body.  Its
    binding contains only identifiers, sequences, hashes, and source refs; a
    consumer must rehydrate any bytes from the canonical authority after the
    injected validator proves the binding still matches.
    """

    __tablename__ = "runtime_terminal_boundary_outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','processing','delivered','dead_letter')",
            name="ck_runtime_terminal_boundary_outbox_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_runtime_terminal_boundary_outbox_attempt_count",
        ),
        CheckConstraint(
            "char_length(binding_sha256) = 64",
            name="ck_runtime_terminal_boundary_outbox_binding_sha",
        ),
        CheckConstraint(
            "char_length(idempotency_key) = 64",
            name="ck_runtime_terminal_boundary_outbox_idempotency_sha",
        ),
        UniqueConstraint(
            "tenant_id",
            "event_kind",
            "authority_ref",
            "authority_id",
            name="uq_runtime_terminal_boundary_authority_event",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_runtime_terminal_boundary_idempotency",
        ),
        Index(
            "ix_runtime_terminal_boundary_claim",
            "tenant_id",
            "status",
            "available_at",
            "lease_expires_at",
        ),
        Index(
            "ix_runtime_terminal_boundary_task",
            "tenant_id",
            "runtime_task_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runtime_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runtime_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    # T0/runtime sessions include governed synthetic identifiers for sibling
    # lanes that do not own a ChatSession row (subagent, early trigger abort,
    # heartbeat skip).  Web processors separately prove their UUID ChatSession.
    session_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    terminal_status: Mapped[str] = mapped_column(String(40), nullable=False)
    authority_ref: Mapped[str] = mapped_column(String(100), nullable=False)
    authority_id: Mapped[str] = mapped_column(String(200), nullable=False)
    binding_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    claimed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_receipt_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
