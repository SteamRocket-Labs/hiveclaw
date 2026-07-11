"""Thin enterprise control index for native AI assets."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AIAssetRecord(Base):
    """Control metadata only; native runtimes remain the content authorities."""

    __tablename__ = "ai_asset_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "asset_type", "native_key", name="uq_ai_asset_native_key"),
        CheckConstraint(
            "asset_type IN ('agent','skill','workflow','subagent','external_capability')",
            name="ck_ai_asset_type",
        ),
        CheckConstraint(
            "lifecycle_status IN ('draft','active','deprecated','revoked','deleted','quarantined')",
            name="ck_ai_asset_lifecycle_status",
        ),
        CheckConstraint(
            "projection_status IN ('pending','applied','failed','drifted')",
            name="ck_ai_asset_projection_status",
        ),
        Index("ix_ai_asset_tenant_type_status", "tenant_id", "asset_type", "lifecycle_status"),
        Index("ix_ai_asset_content_hash", "content_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    native_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    native_key: Mapped[str] = mapped_column(String(500), nullable=False)
    native_locator_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    display_name: Mapped[str] = mapped_column(String(300), nullable=False)

    owner_type: Mapped[str] = mapped_column(String(30), nullable=False, default="tenant")
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    visibility_scope: Mapped[str] = mapped_column(String(30), nullable=False, default="tenant")
    lifecycle_status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft", index=True)

    active_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("config_revisions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, default="native")
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    trust_state: Mapped[str] = mapped_column(String(30), nullable=False, default="trusted", index=True)
    dependencies_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    compatibility_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    admission_state: Mapped[str] = mapped_column(String(30), nullable=False, default="admitted", index=True)
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    usage_evidence_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    projection_status: Mapped[str] = mapped_column(String(30), nullable=False, default="applied")
    projection_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AIAssetUsageEvent(Base):
    """Exactly-once, revision-bound evidence of a real asset consumption."""

    __tablename__ = "ai_asset_usage_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "asset_id", "idempotency_key", name="uq_ai_asset_usage_event_idempotency"),
        Index("ix_ai_asset_usage_events_asset_created", "asset_id", "created_at"),
        Index("ix_ai_asset_usage_events_tenant_kind", "tenant_id", "usage_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_asset_records.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("config_revisions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    revision_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    native_key: Mapped[str] = mapped_column(String(500), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_kind: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    usage_units: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    idempotency_key: Mapped[str] = mapped_column(String(500), nullable=False)
    runtime_task_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    span_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    evidence_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
