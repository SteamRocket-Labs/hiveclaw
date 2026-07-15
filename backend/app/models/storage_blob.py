"""Tenant-scoped authority records for immutable storage blobs and GC."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StorageBlob(Base):
    """One immutable, verified provider object within a tenant crypto boundary."""

    __tablename__ = "storage_blobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_storage_blobs_tenant_id_id"),
        UniqueConstraint("tenant_id", "content_sha256", name="uq_storage_blobs_tenant_digest"),
        UniqueConstraint("provider", "bucket", "object_key", name="uq_storage_blobs_provider_location"),
        CheckConstraint("scope_type IN ('tenant','system')", name="ck_storage_blobs_scope_type"),
        CheckConstraint(
            "state IN ('uploading','available','quarantined','deleting','deleted','failed')",
            name="ck_storage_blobs_state",
        ),
        CheckConstraint("char_length(content_sha256) = 64", name="ck_storage_blobs_sha256_length"),
        CheckConstraint("size_bytes >= 0", name="ck_storage_blobs_size_nonnegative"),
        Index("ix_storage_blobs_tenant_state_delete_after", "tenant_id", "state", "delete_after"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False, default="tenant", server_default="tenant")
    kind: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    retention_class: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="uploading", index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    bucket: Mapped[str] = mapped_column(String(200), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    encryption_key_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delete_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class StorageBlobRef(Base):
    """Authoritative ownership/pin edge; filenames never determine liveness."""

    __tablename__ = "storage_blob_refs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "blob_id"],
            ["storage_blobs.tenant_id", "storage_blobs.id"],
            ondelete="CASCADE",
            name="fk_storage_blob_refs_tenant_blob",
        ),
        UniqueConstraint(
            "tenant_id",
            "blob_id",
            "owner_type",
            "owner_id",
            "purpose",
            name="uq_storage_blob_refs_owner_purpose",
        ),
        Index("ix_storage_blob_refs_tenant_owner", "tenant_id", "owner_type", "owner_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    blob_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    owner_type: Mapped[str] = mapped_column(String(60), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(300), nullable=False)
    purpose: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    pinned_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    legal_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class StorageGCRun(Base):
    """Tenant-scoped immutable manifest and execution receipt ledger."""

    __tablename__ = "storage_gc_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "manifest_sha256", "mode", name="uq_storage_gc_runs_manifest_mode"),
        CheckConstraint(
            "mode IN ('inventory','backfill','dry_run','quarantine','sweep','restore')",
            name="ck_storage_gc_runs_mode",
        ),
        CheckConstraint(
            "status IN ('running','completed','partial','failed')",
            name="ck_storage_gc_runs_status",
        ),
        CheckConstraint("char_length(manifest_sha256) = 64", name="ck_storage_gc_runs_sha256_length"),
        Index("ix_storage_gc_runs_tenant_started", "tenant_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running")
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    candidate_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    candidate_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    processed_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    processed_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    receipt_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
