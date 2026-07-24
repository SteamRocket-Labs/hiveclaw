"""Company Knowledge authority, evidence, publication, and recovery records.

These aggregates are deliberately separate from Personal Knowledge authority.
They may reference the shared ``knowledge_*`` content/index core, but Company
publication state, reviews, evidence, events, and recovery are tenant-owned
database truth.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
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


_SENSITIVITY_CHECK = "sensitivity IN ('PL1_public','PL2_pii','PL3_sensitive','PL4_credential')"


class CompanyKnowledgeSourceContract(Base):
    """Immutable version of a tenant source's identity, ACL, and time contract."""

    __tablename__ = "company_knowledge_source_contracts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "stable_source_id",
            "version",
            name="uq_company_knowledge_source_contract_version",
        ),
        CheckConstraint(
            "status IN ('draft','active','retired','blocked_drift')",
            name="ck_company_knowledge_source_contract_status",
        ),
        CheckConstraint(
            "ingest_mode IN ('manual','snapshot','incremental','cdc','webhook','reference')",
            name="ck_company_knowledge_source_contract_ingest_mode",
        ),
        CheckConstraint(
            "default_sensitivity IN ('PL1_public','PL2_pii','PL3_sensitive','PL4_credential')",
            name="ck_company_knowledge_source_contract_sensitivity",
        ),
        Index(
            "ix_company_knowledge_source_contract_active",
            "tenant_id",
            "stable_source_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft", index=True)
    source_kind: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    provider_kind: Mapped[str] = mapped_column(String(80), nullable=False, default="native")
    stable_source_id: Mapped[str] = mapped_column(String(300), nullable=False)
    owner_principal_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    accountable_steward_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    connection_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    schema_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    schema_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    identity_keys_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    relation_keys_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    ingest_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    cursor_kind: Mapped[str | None] = mapped_column(String(80), nullable=True)
    cursor_policy_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    watermark_field: Mapped[str | None] = mapped_column(String(300), nullable=True)
    temporal_mapping_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    source_acl_mapping_policy_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    default_sensitivity: Mapped[str] = mapped_column(
        String(30), nullable=False, default="PL1_public", server_default="PL1_public"
    )
    export_policy_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    retention_policy_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    legal_hold_policy_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    allowed_namespaces_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    precedence_policy_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    acceptance_suite_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    idempotency_policy_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    contract_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reviewed_by_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CompanyKnowledgeSource(Base):
    """Current source-item lineage and source-authority snapshot."""

    __tablename__ = "company_knowledge_sources"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_contract_id",
            "source_item_id",
            "source_revision",
            name="uq_company_knowledge_source_revision",
        ),
        CheckConstraint(
            "status IN ('registered','ingested','blocked_source_authority','drifted','retired')",
            name="ck_company_knowledge_source_status",
        ),
        CheckConstraint(_SENSITIVITY_CHECK, name="ck_company_knowledge_source_sensitivity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_knowledge_source_contracts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_item_id: Mapped[str] = mapped_column(String(500), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    namespace: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    sensitivity: Mapped[str] = mapped_column(String(30), nullable=False)
    canonical_artifact_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_acl_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_acl_snapshot_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    retention_state_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    legal_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    cursor_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    lineage_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="registered", index=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_type: Mapped[str] = mapped_column(String(30), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CompanyKnowledgeEvidence(Base):
    """Lossless canonical evidence envelope; never a generated semantic summary."""

    __tablename__ = "company_knowledge_evidence"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_company_knowledge_evidence_idempotency"),
        CheckConstraint(
            "evidence_kind IN ('document','structured_record','event','living_object_revision','external_immutable_ref')",
            name="ck_company_knowledge_evidence_kind",
        ),
        CheckConstraint(
            "status IN ('accepted','blocked_source_authority','quarantined','revoked')",
            name="ck_company_knowledge_evidence_status",
        ),
        Index("ix_company_knowledge_evidence_source_created", "tenant_id", "source_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_knowledge_sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_knowledge_source_contracts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_item_id: Mapped[str] = mapped_column(String(500), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    artifact_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    schema_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    typed_payload_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    canonical_envelope_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_acl_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_acl_snapshot_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cursor_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sequence: Mapped[str | None] = mapped_column(String(300), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    coverage_ledger_ref: Mapped[str] = mapped_column(String(1000), nullable=False)
    coverage_ledger_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    ingestion_receipt_ref: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="accepted", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CompanyKnowledgeImportJob(Base):
    """Restart-resumable conversion of one lossless source item into evidence."""

    __tablename__ = "company_knowledge_import_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_company_knowledge_import_job_idempotency"),
        CheckConstraint(
            "status IN ('queued','running','completed','held','failed','cancelled')",
            name="ck_company_knowledge_import_job_status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0",
            name="ck_company_knowledge_import_job_attempts",
        ),
        Index(
            "ix_company_knowledge_import_job_claim",
            "tenant_id",
            "status",
            "available_at",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_knowledge_source_contracts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    artifact_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued", index=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    last_error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_knowledge_sources.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_knowledge_evidence.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_documents.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "company_knowledge_proposals.id",
            ondelete="RESTRICT",
            name="fk_company_knowledge_import_job_proposal",
        ),
        nullable=True,
        index=True,
    )
    created_by_type: Mapped[str] = mapped_column(String(30), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    accountable_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CompanyKnowledgeProposal(Base):
    """Governed Knowledge/Ontology candidate; never published truth by itself."""

    __tablename__ = "company_knowledge_proposals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_company_knowledge_proposal_idempotency"),
        UniqueConstraint(
            "tenant_id",
            "materialization_idempotency_key",
            name="uq_company_knowledge_proposal_materialization_idempotency",
        ),
        CheckConstraint(
            "proposal_kind IN ('knowledge','ontology','combined','personal_promotion','living_object','legacy_import')",
            name="ck_company_knowledge_proposal_kind",
        ),
        CheckConstraint(
            "status IN ('draft','submitted','in_review','changes_requested','approved','rejected','withdrawn',"
            "'publishing','published','publish_failed','superseded','retired','restored')",
            name="ck_company_knowledge_proposal_status",
        ),
        CheckConstraint(
            _SENSITIVITY_CHECK.replace("sensitivity", "proposed_sensitivity"), name="ck_company_kp_sensitivity"
        ),
        Index("ix_company_knowledge_proposal_queue", "tenant_id", "status", "submitted_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    proposal_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_knowledge_sources.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_documents.id", ondelete="RESTRICT"), nullable=True
    )
    source_revision_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    baseline_publication_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "company_knowledge_publications.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_company_knowledge_proposal_baseline_publication",
        ),
        nullable=True,
    )
    baseline_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    materialized_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "knowledge_documents.id",
            ondelete="RESTRICT",
            name="fk_company_knowledge_proposal_materialized_document",
        ),
        nullable=True,
        index=True,
    )
    materialization_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    materialization_receipt_json: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    materialization_idempotency_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    materialized_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_company_knowledge_proposal_materialized_by",
        ),
        nullable=True,
    )
    materialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    proposed_patch_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    proposed_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_namespace: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    proposed_sensitivity: Mapped[str] = mapped_column(String(30), nullable=False)
    source_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    source_coverage_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    conflict_candidates_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    ontology_mapping_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="draft", index=True)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False, default="normal")
    required_review_policy_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by_type: Mapped[str] = mapped_column(String(30), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    accountable_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CompanyKnowledgeReview(Base):
    """Append-only reviewer decision and evidence receipt."""

    __tablename__ = "company_knowledge_reviews"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "proposal_id",
            "reviewer_user_id",
            "review_round",
            name="uq_company_knowledge_review_round",
        ),
        CheckConstraint(
            "decision IN ('approve','reject','request_changes','abstain')",
            name="ck_company_knowledge_review_decision",
        ),
        Index("ix_company_knowledge_review_proposal_created", "tenant_id", "proposal_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_knowledge_proposals.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reviewer_role: Mapped[str] = mapped_column(String(80), nullable=False)
    review_round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    subject_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    policy_snapshot_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CompanyKnowledgePublication(Base):
    """Immutable Company Knowledge version; active status selects current truth."""

    __tablename__ = "company_knowledge_publications"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "logical_resource_key",
            "version",
            name="uq_company_knowledge_publication_version",
        ),
        CheckConstraint(
            "status IN ('active','superseded','retired','revoked')",
            name="ck_company_knowledge_publication_status",
        ),
        CheckConstraint(_SENSITIVITY_CHECK, name="ck_company_knowledge_publication_sensitivity"),
        Index(
            "ix_company_knowledge_publication_active",
            "tenant_id",
            "logical_resource_key",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_company_knowledge_publication_discovery",
            "tenant_id",
            "namespace",
            "status",
            "valid_from",
            "valid_until",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    logical_resource_key: Mapped[str] = mapped_column(String(500), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_documents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    artifact_ref: Mapped[str] = mapped_column(String(1000), nullable=False)
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_knowledge_proposals.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    review_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    namespace: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    sensitivity: Mapped[str] = mapped_column(String(30), nullable=False)
    permission_resource_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    source_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    evidence_bundle_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active", index=True)
    supersedes_publication_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_knowledge_publications.id", ondelete="RESTRICT"), nullable=True
    )
    rollback_ref: Mapped[str] = mapped_column(String(1000), nullable=False)
    companion_release_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    restored_from_publication_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_knowledge_publications.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CompanyKnowledgeEvent(Base):
    """Append-only Company Knowledge domain evidence."""

    __tablename__ = "company_knowledge_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_company_knowledge_event_idempotency"),
        UniqueConstraint("tenant_id", "stream_sequence", name="uq_company_knowledge_event_stream_sequence"),
        Index("ix_company_knowledge_event_tenant_sequence", "tenant_id", "stream_sequence"),
        Index("ix_company_knowledge_event_stream", "tenant_id", "resource_type", "resource_id", "created_at"),
        Index("ix_company_knowledge_event_trace", "tenant_id", "trace_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    accountable_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resource_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_snapshot_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    trace_id: Mapped[str] = mapped_column(String(300), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    stream_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CompanyKnowledgeOutbox(Base):
    """Durable idempotent projection/index work emitted with authority commits."""

    __tablename__ = "company_knowledge_outbox"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_company_knowledge_outbox_idempotency"),
        CheckConstraint(
            "status IN ('pending','processing','completed','failed','cancelled')",
            name="ck_company_knowledge_outbox_status",
        ),
        Index("ix_company_knowledge_outbox_claim", "status", "available_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    aggregate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_knowledge_events.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=8, server_default="8")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
