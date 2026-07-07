"""Personal and organization knowledge-base records.

The first implementation is PostgreSQL-native: canonical Markdown stays on
disk, while these tables provide searchable slices, relationship metadata, and
permission grants. Vector indexes are intentionally not required for Personal
KB so local and production Postgres deployments do not need pgvector to boot.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class KnowledgeDocument(Base):
    """One canonical Markdown artifact inside a person/team/org scope."""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "scope_type", "scope_id", "source_sha256", name="uq_knowledge_document_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope_type: Mapped[str] = mapped_column(String(30), nullable=False, default="person", index=True)
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    artifact_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending", index=True)
    sensitivity: Mapped[str] = mapped_column(String(30), nullable=False, default="internal", index=True)
    agent_searchable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    canonical_md_path: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_md_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    doc_metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class KnowledgeSegment(Base):
    """Searchable chunk derived from a canonical knowledge document."""

    __tablename__ = "knowledge_segments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "document_id", "position", name="uq_knowledge_segment_position"),
        UniqueConstraint("tenant_id", "document_id", "segment_hash", name="uq_knowledge_segment_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope_type: Mapped[str] = mapped_column(String(30), nullable=False, default="person", index=True)
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    heading_path_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tsv: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    segment_metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class KnowledgeEntity(Base):
    """Canonical entity projection extracted from person-scope knowledge."""

    __tablename__ = "knowledge_entities"
    __table_args__ = (
        UniqueConstraint("tenant_id", "scope_type", "scope_id", "entity_type", "canonical_name", name="uq_knowledge_entity_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope_type: Mapped[str] = mapped_column(String(30), nullable=False, default="person", index=True)
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    canonical_name: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False, default="freeform", index=True)
    aliases_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    merged_into_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_entities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class KnowledgeAssertion(Base):
    """Subject-predicate-object assertion with source segment references."""

    __tablename__ = "knowledge_assertions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "scope_type",
            "scope_id",
            "subject_text",
            "predicate",
            "object_text",
            name="uq_knowledge_assertion_text",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope_type: Mapped[str] = mapped_column(String(30), nullable=False, default="person", index=True)
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    subject_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_entities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    subject_text: Mapped[str] = mapped_column(String(300), nullable=False)
    predicate: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    object_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_entities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    object_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active", index=True)
    source_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class KnowledgeLink(Base):
    """Typed graph edge between documents, segments, entities, or assertions."""

    __tablename__ = "knowledge_links"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "scope_type",
            "scope_id",
            "from_kind",
            "from_id",
            "to_kind",
            "to_id",
            "relation",
            name="uq_knowledge_link_edge",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope_type: Mapped[str] = mapped_column(String(30), nullable=False, default="person", index=True)
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    from_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    from_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    to_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    to_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    relation: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    source_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class KnowledgeIndexJob(Base):
    """Asynchronous indexing pipeline state for one canonical document."""

    __tablename__ = "knowledge_index_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "document_id", "artifact_hash", name="uq_knowledge_index_job_artifact"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope_type: Mapped[str] = mapped_column(String(30), nullable=False, default="person", index=True)
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    artifact_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(50), nullable=False, default="queued", index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="queued", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    job_metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class KnowledgeGrant(Base):
    """Permission edge for owner, agent, user, team, or org access to knowledge."""

    __tablename__ = "knowledge_grants"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "scope_type",
            "scope_id",
            "resource_type",
            "resource_id",
            "grantee_type",
            "grantee_id",
            "permission",
            name="uq_knowledge_grant_resource_grantee",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope_type: Mapped[str] = mapped_column(String(30), nullable=False, default="person", index=True)
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(30), nullable=False, default="scope", index=True)
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    grantee_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    grantee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    permission: Mapped[str] = mapped_column(String(30), nullable=False, default="search", index=True)
    grant_metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
