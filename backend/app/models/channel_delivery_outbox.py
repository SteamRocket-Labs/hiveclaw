"""Durable terminal text and artifact delivery intents for external channels."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.services.channel_secret_storage import EncryptedDeliveryTargetJSON


class ChannelDeliveryOutbox(Base):
    """One immutable terminal result addressed to one channel target snapshot.

    RuntimeTask and transcript rows remain execution truth. This row is the
    retryable delivery bridge. Per-part receipts make text plus attachment
    delivery resumable without replaying already acknowledged provider calls.
    """

    __tablename__ = "channel_delivery_outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','processing','delivered','dead_letter','needs_reconciliation')",
            name="ck_channel_delivery_outbox_status",
        ),
        CheckConstraint(
            "delivery_kind IN ('terminal_result','interactive_prompt')",
            name="ck_channel_delivery_outbox_kind",
        ),
        UniqueConstraint(
            "tenant_id",
            "runtime_task_id",
            "delivery_kind",
            "target_hash",
            name="uq_channel_delivery_outbox_runtime_target",
        ),
        Index("ix_channel_delivery_outbox_claim", "status", "available_at", "locked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    runtime_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    external_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("external_principals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Snapshot evidence rather than a live FK: deleting/replacing a channel
    # config must leave the original target identity available for dead-letter
    # diagnosis and must not silently retarget queued output.
    channel_config_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    target_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="terminal_result", server_default=text("'terminal_result'")
    )
    terminal_status: Mapped[str] = mapped_column(String(40), nullable=False)
    delivery_target_json: Mapped[dict] = mapped_column(
        EncryptedDeliveryTargetJSON(postgres_jsonb=True),
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    text_content: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_ids_json: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    delivery_receipts_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
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
