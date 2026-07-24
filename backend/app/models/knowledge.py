"""Shared Personal and Company Knowledge content/index records.

Personal authority remains in ``KnowledgeGrant`` and
``PersonalKnowledgeProposal``. Company authority lives in the separate
``company_knowledge_*`` aggregates; Company content uses ``scope_type=company``
and ``scope_id=tenant_id`` here only after a reviewed publication transaction.
Vector indexes remain optional derived projections, so local and production
Postgres do not need pgvector to boot.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class KnowledgeDocument(Base):
    """One canonical document artifact shared by Personal and Company scopes."""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "scope_type", "scope_id", "source_sha256", name="uq_knowledge_document_source"),
        CheckConstraint(
            "sensitivity IN ('PL1_public','PL2_pii','PL3_sensitive','PL4_credential')",
            name="ck_knowledge_documents_sensitivity",
        ),
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
    sensitivity: Mapped[str] = mapped_column(
        String(30), nullable=False, default="PL1_public", server_default="PL1_public", index=True
    )
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
    """Rebuildable entity projection extracted from a scoped document."""

    __tablename__ = "knowledge_entities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "scope_type", "scope_id", "entity_type", "canonical_name", name="uq_knowledge_entity_name"
        ),
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
    """Personal Knowledge permission edge with authenticated runtime bindings."""

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
            "binding_key",
            name="uq_knowledge_grant_resource_grantee",
        ),
        CheckConstraint(
            "sensitivity_ceiling IN ('PL1_public','PL2_pii','PL3_sensitive','PL4_credential')",
            name="ck_knowledge_grant_sensitivity_ceiling",
        ),
        CheckConstraint(
            "grantee_type != 'agent' OR revoked_at IS NOT NULL OR ("
            "requester_user_id IS NOT NULL AND expires_at IS NOT NULL "
            "AND ((purpose = 'autonomous_agent' AND requester_user_id = scope_id "
            "AND session_id IS NULL AND delegation_id IS NULL) "
            "OR (purpose = 'interactive_session' AND session_id IS NOT NULL AND delegation_id IS NULL) "
            "OR (purpose IN ('a2a_delegation','subagent_delegation') "
            "AND session_id IS NOT NULL AND delegation_id IS NOT NULL)))",
            name="ck_knowledge_grant_agent_binding",
        ),
        CheckConstraint(
            "revoked_at IS NOT NULL OR ((resource_type = 'scope' AND resource_id = scope_id "
            "AND document_id IS NULL) OR (resource_type = 'document' AND document_id IS NOT NULL "
            "AND resource_id = document_id))",
            name="ck_knowledge_grant_resource_binding",
        ),
        CheckConstraint(
            "revoked_by_user_id IS NULL OR revoked_at IS NOT NULL",
            name="ck_knowledge_grant_revoke_actor",
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
    requester_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    purpose: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    delegation_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    sensitivity_ceiling: Mapped[str] = mapped_column(
        String(30), nullable=False, default="PL1_public", server_default="PL1_public", index=True
    )
    binding_key: Mapped[str] = mapped_column(
        String(200), nullable=False, default=lambda: f"compat:{uuid.uuid4().hex}", index=True
    )
    grant_metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PersonalKnowledgeProposal(Base):
    """Governed Agent-authored candidate for an owner's Personal KB."""

    __tablename__ = "personal_knowledge_proposals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_personal_kb_proposal_idempotency"),
        Index("ix_personal_kb_proposals_owner_status", "owner_user_id", "status"),
        Index("ix_personal_kb_proposals_agent_created", "proposed_by_agent_id", "created_at"),
        CheckConstraint(
            "status IN ('pending','approved','rejected','committed','failed')",
            name="ck_personal_kb_proposal_status",
        ),
        CheckConstraint(
            "policy_outcome IN ('approve','ask','reject')",
            name="ck_personal_kb_proposal_policy_outcome",
        ),
        CheckConstraint(
            "sensitivity IN ('PL1_public','PL2_pii','PL3_sensitive','PL4_credential')",
            name="ck_personal_kb_proposal_sensitivity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    proposed_by_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delegated_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    delegation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    proposed_content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    baseline_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_documents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    baseline_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("config_revisions.id", ondelete="SET NULL"), nullable=True
    )
    baseline_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diff_unified: Mapped[str] = mapped_column(Text, nullable=False)
    target_collection: Mapped[str] = mapped_column(String(120), nullable=False, default="inbox")
    source_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    sensitivity: Mapped[str] = mapped_column(String(30), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    policy_outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    policy_reason_codes_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_documents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("config_revisions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    rollback_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
