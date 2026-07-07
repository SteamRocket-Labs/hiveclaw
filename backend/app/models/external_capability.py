"""External capability Trust Gate records.

These tables are the admission ledger for third-party Skills, MCP servers,
plugins, and subagents. They are intentionally upstream of runtime activation:
approved snapshots can later be projected into existing Skill/MCP/Plugin
runtime surfaces, but review records never execute anything by themselves.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ExternalCapabilityReview(Base):
    """One staged external capability pending Trust Gate admission."""

    __tablename__ = "external_capability_reviews"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_format", "source_uri", "source_hash", name="uq_external_review_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_format: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="review_required", index=True)
    admission_class: Mapped[str] = mapped_column(String(40), nullable=False, default="governed_runtime", index=True)
    admission_report_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    governance_projection_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    normalized_manifest_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ExternalCapabilitySnapshot(Base):
    """Immutable-ish approved source snapshot used by later activation."""

    __tablename__ = "external_capability_snapshots"
    __table_args__ = (
        UniqueConstraint("tenant_id", "snapshot_key", name="uq_external_snapshot_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("external_capability_reviews.id", ondelete="CASCADE"), nullable=False, index=True
    )
    snapshot_key: Mapped[str] = mapped_column(String(260), nullable=False)
    source_format: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="approved", index=True)
    admission_class: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    admission_report_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    governance_projection_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    component_manifest_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ExternalExtensionActivation(Base):
    """Agent-scoped activation of one approved external capability snapshot."""

    __tablename__ = "external_extension_activations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", "snapshot_id", name="uq_external_extension_activation"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("external_capability_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active", index=True)
    component_types_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    activation_result_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    activated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
