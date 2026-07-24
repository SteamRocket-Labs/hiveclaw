"""Tenant-owned Company Ontology authority and immutable release records.

Domain packages and engines are replaceable inputs/derivations. These rows
remain the Hive authority for installed definitions, curation evidence,
published releases, typed facts, validity, and rollback lineage.
"""

from __future__ import annotations

import uuid
from datetime import datetime

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


_SENSITIVITY_CHECK = "sensitivity IN ('PL1_public','PL2_pii','PL3_sensitive','PL4_credential')"
_DEFINITION_STATUS_CHECK = "status IN ('candidate','active','superseded','retired','revoked')"


class CompanyOntologyPackage(Base):
    """Stable logical Domain Pack identity."""

    __tablename__ = "company_ontology_packages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "package_key", name="uq_company_ontology_package_key"),
        CheckConstraint(
            "status IN ('available','retired','blocked')",
            name="ck_company_ontology_package_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    package_key: Mapped[str] = mapped_column(String(240), nullable=False)
    display_name: Mapped[str] = mapped_column(String(300), nullable=False)
    publisher: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="available", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CompanyOntologyPackageVersion(Base):
    """Immutable content-addressed declarative package version."""

    __tablename__ = "company_ontology_package_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "package_id", "version", name="uq_company_ontology_package_version"),
        UniqueConstraint("tenant_id", "content_hash", name="uq_company_ontology_package_content_hash"),
        CheckConstraint(
            "admission_status IN ('pending','admitted','rejected','incompatible')",
            name="ck_company_ontology_package_admission",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    package_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_packages.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version: Mapped[str] = mapped_column(String(120), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(1000), nullable=False)
    signature_key_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    hive_contract_version: Mapped[str] = mapped_column(String(120), nullable=False)
    engine_capabilities_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    namespaces_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    dependencies_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    conflicts_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    manifest_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    schema_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    mappings_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    rules_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    queries_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    actions_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    permissions_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    acceptance_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    migrations_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    admission_status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    admission_receipt_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CompanyOntologyPackageInstallation(Base):
    """Tenant installation; installation never implies activation or release."""

    __tablename__ = "company_ontology_package_installations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "package_version_id",
            name="uq_company_ontology_package_installation",
        ),
        CheckConstraint(
            "status IN ('installed','blocked','uninstalled')",
            name="ck_company_ontology_package_installation_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    package_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_ontology_package_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="installed", index=True)
    requested_capabilities_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    compatibility_receipt_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    acceptance_receipt_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    installed_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CompanyOntologyActivation(Base):
    """Reviewed tenant activation of one installed package version."""

    __tablename__ = "company_ontology_activations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "namespace",
            "activation_version",
            name="uq_company_ontology_activation_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_company_ontology_activation_idempotency",
        ),
        CheckConstraint(
            "status IN ('draft','dry_run_passed','active','superseded','retired','blocked')",
            name="ck_company_ontology_activation_status",
        ),
        Index(
            "ix_company_ontology_activation_active",
            "tenant_id",
            "namespace",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    installation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_ontology_package_installations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    namespace: Mapped[str] = mapped_column(String(300), nullable=False)
    activation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    configuration_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    dry_run_receipt_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft", index=True)
    supersedes_activation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_activations.id", ondelete="RESTRICT"), nullable=True
    )
    activated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CompanyOntologyCurationRun(Base):
    """Recoverable LLM-primary semantic candidate run."""

    __tablename__ = "company_ontology_curation_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_company_ontology_curation_idempotency"),
        CheckConstraint(
            "status IN ('pending','running','checkpointed','held','quarantined','completed','failed','cancelled')",
            name="ck_company_ontology_curation_status",
        ),
        Index("ix_company_ontology_curation_recovery", "tenant_id", "status", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    activation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_activations.id", ondelete="RESTRICT"), nullable=False
    )
    baseline_release_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "company_ontology_releases.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_company_ontology_curation_baseline_release",
        ),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    source_contract_versions_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    evidence_scope_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    authority_snapshot_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    requested_operations_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    model_prompt_receipts_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    candidate_patch_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    candidate_patch_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidate_patch_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    coverage_ledger_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    conflict_ledger_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    unresolved_questions_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    acceptance_result_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    retry_state_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    checkpoint_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_by_type: Mapped[str] = mapped_column(String(30), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    accountable_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CompanyOntologyRelease(Base):
    """Immutable published ontology release, distinct from Knowledge publication."""

    __tablename__ = "company_ontology_releases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "namespace", "version", name="uq_company_ontology_release_version"),
        CheckConstraint(
            "status IN ('active','superseded','retired','revoked','publish_failed')",
            name="ck_company_ontology_release_status",
        ),
        Index(
            "ix_company_ontology_release_active",
            "tenant_id",
            "namespace",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    namespace: Mapped[str] = mapped_column(String(300), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    activation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_activations.id", ondelete="RESTRICT"), nullable=False
    )
    package_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_ontology_package_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    curation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_curation_runs.id", ondelete="RESTRICT"), nullable=False
    )
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_knowledge_proposals.id", ondelete="RESTRICT"), nullable=False
    )
    release_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    review_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_coverage_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    conflict_ledger_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    unresolved_questions_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    deterministic_validation_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    semantic_review_receipts_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    acceptance_result_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    migration_plan_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    rollback_ref: Mapped[str] = mapped_column(String(1000), nullable=False)
    projection_rebuild_plan_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active", index=True)
    supersedes_release_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_releases.id", ondelete="RESTRICT"), nullable=True
    )
    restored_from_release_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_releases.id", ondelete="RESTRICT"), nullable=True
    )
    published_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CompanyOntologyObjectType(Base):
    __tablename__ = "company_ontology_object_types"
    __table_args__ = (
        UniqueConstraint("tenant_id", "release_id", "type_ref", name="uq_company_ontology_object_type"),
        CheckConstraint(_DEFINITION_STATUS_CHECK, name="ck_company_ontology_object_type_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_releases.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    namespace: Mapped[str] = mapped_column(String(300), nullable=False)
    type_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    schema_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    sensitivity: Mapped[str] = mapped_column(String(30), nullable=False, default="PL1_public")
    permission_resource_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CompanyOntologyPropertyType(Base):
    __tablename__ = "company_ontology_property_types"
    __table_args__ = (
        UniqueConstraint("tenant_id", "release_id", "property_ref", name="uq_company_ontology_property_type"),
        CheckConstraint(_DEFINITION_STATUS_CHECK, name="ck_company_ontology_property_type_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_releases.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    namespace: Mapped[str] = mapped_column(String(300), nullable=False)
    property_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    owner_type_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    value_schema_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    cardinality_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    source_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    sensitivity: Mapped[str] = mapped_column(String(30), nullable=False, default="PL1_public")
    permission_resource_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CompanyOntologyLinkType(Base):
    __tablename__ = "company_ontology_link_types"
    __table_args__ = (
        UniqueConstraint("tenant_id", "release_id", "link_type_ref", name="uq_company_ontology_link_type"),
        CheckConstraint(_DEFINITION_STATUS_CHECK, name="ck_company_ontology_link_type_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_releases.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    namespace: Mapped[str] = mapped_column(String(300), nullable=False)
    link_type_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    from_type_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    to_type_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    schema_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    source_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    sensitivity: Mapped[str] = mapped_column(String(30), nullable=False, default="PL1_public")
    permission_resource_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CompanyOntologyEventType(Base):
    __tablename__ = "company_ontology_event_types"
    __table_args__ = (
        UniqueConstraint("tenant_id", "release_id", "event_type_ref", name="uq_company_ontology_event_type"),
        CheckConstraint(_DEFINITION_STATUS_CHECK, name="ck_company_ontology_event_type_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_releases.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    namespace: Mapped[str] = mapped_column(String(300), nullable=False)
    event_type_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    schema_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    sensitivity: Mapped[str] = mapped_column(String(30), nullable=False, default="PL1_public")
    permission_resource_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CompanyOntologyRuleDefinition(Base):
    __tablename__ = "company_ontology_rule_definitions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "release_id", "rule_ref", name="uq_company_ontology_rule_definition"),
        CheckConstraint(
            "evaluation_mode IN ('deterministic_typed','llm_semantic_candidate','human_decision',"
            "'external_authoritative_result')",
            name="ck_company_ontology_rule_evaluation",
        ),
        CheckConstraint(_DEFINITION_STATUS_CHECK, name="ck_company_ontology_rule_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_releases.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    namespace: Mapped[str] = mapped_column(String(300), nullable=False)
    rule_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    rule_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    owner_principal_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    scope_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_schema_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    output_schema_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    examples_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    counterexamples_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    risk: Mapped[str] = mapped_column(String(40), nullable=False)
    review_policy_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    conflict_precedence_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    evaluation_mode: Mapped[str] = mapped_column(String(50), nullable=False)
    acceptance_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(30), nullable=False, default="PL1_public")
    permission_resource_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CompanyOntologyActionType(Base):
    __tablename__ = "company_ontology_action_types"
    __table_args__ = (
        UniqueConstraint("tenant_id", "release_id", "action_type_ref", name="uq_company_ontology_action_type"),
        CheckConstraint(_DEFINITION_STATUS_CHECK, name="ck_company_ontology_action_type_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_releases.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    namespace: Mapped[str] = mapped_column(String(300), nullable=False)
    action_type_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    input_schema_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    output_schema_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    required_capability: Mapped[str] = mapped_column(String(300), nullable=False)
    tool_workflow_mapping_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    approval_policy_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    side_effect_classification: Mapped[str] = mapped_column(String(80), nullable=False)
    simulation_contract_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    sensitivity: Mapped[str] = mapped_column(String(30), nullable=False, default="PL1_public")
    permission_resource_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CompanyOntologyObject(Base):
    """Stable object identity with typed properties projected per release."""

    __tablename__ = "company_ontology_objects"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "release_id",
            "stable_object_key",
            name="uq_company_ontology_object_release_key",
        ),
        CheckConstraint(_SENSITIVITY_CHECK, name="ck_company_ontology_object_sensitivity"),
        CheckConstraint(
            "status IN ('candidate','active','held','superseded','retired','revoked')",
            name="ck_company_ontology_object_status",
        ),
        Index("ix_company_ontology_object_type_status", "tenant_id", "object_type_ref", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stable_object_key: Mapped[str] = mapped_column(String(500), nullable=False)
    namespace: Mapped[str] = mapped_column(String(300), nullable=False)
    object_type_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    display_name: Mapped[str] = mapped_column(String(500), nullable=False)
    properties_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_releases.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(30), nullable=False)
    permission_resource_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    supersedes_object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_objects.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CompanyOntologyObjectIdentity(Base):
    """Source keys, aliases, and non-destructive merge/split lineage."""

    __tablename__ = "company_ontology_object_identities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "object_id",
            "source_contract_id",
            "source_identity_key",
            name="uq_company_ontology_source_identity_object",
        ),
        CheckConstraint(
            "status IN ('active','merged','split','held','revoked')",
            name="ck_company_ontology_object_identity_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_objects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_knowledge_source_contracts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_identity_key: Mapped[str] = mapped_column(String(700), nullable=False)
    aliases_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    lineage_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    curation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_curation_runs.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CompanyOntologyAssertion(Base):
    """Evidence-bound fact; property values are never silently overwritten."""

    __tablename__ = "company_ontology_assertions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "release_id",
            "stable_assertion_key",
            name="uq_company_ontology_assertion_release_key",
        ),
        CheckConstraint(
            "assertion_kind IN ('sourced','derived','tenant_authored')",
            name="ck_company_ontology_assertion_kind",
        ),
        CheckConstraint(
            "status IN ('candidate','active','superseded','held','revoked')",
            name="ck_company_ontology_assertion_status",
        ),
        CheckConstraint(_SENSITIVITY_CHECK, name="ck_company_ontology_assertion_sensitivity"),
        Index("ix_company_ontology_assertion_subject", "tenant_id", "subject_object_id", "predicate_ref", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stable_assertion_key: Mapped[str] = mapped_column(String(500), nullable=False)
    subject_object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_objects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    predicate_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_objects.id", ondelete="RESTRICT"), nullable=True
    )
    typed_value_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    assertion_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_bundle_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    derived_by_rule_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    curation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_curation_runs.id", ondelete="RESTRICT"), nullable=False
    )
    release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_releases.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sensitivity: Mapped[str] = mapped_column(String(30), nullable=False)
    permission_resource_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="candidate", index=True)
    supersedes_assertion_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_assertions.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CompanyOntologyLink(Base):
    __tablename__ = "company_ontology_links"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "release_id",
            "stable_link_key",
            name="uq_company_ontology_link_release_key",
        ),
        CheckConstraint(
            "status IN ('candidate','active','superseded','held','revoked')",
            name="ck_company_ontology_link_status",
        ),
        CheckConstraint(_SENSITIVITY_CHECK, name="ck_company_ontology_link_sensitivity"),
        Index("ix_company_ontology_link_from", "tenant_id", "from_object_id", "link_type_ref", "status"),
        Index("ix_company_ontology_link_to", "tenant_id", "to_object_id", "link_type_ref", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stable_link_key: Mapped[str] = mapped_column(String(500), nullable=False)
    link_type_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    from_object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_objects.id", ondelete="RESTRICT"), nullable=False
    )
    to_object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_objects.id", ondelete="RESTRICT"), nullable=False
    )
    properties_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_bundle_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    curation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_curation_runs.id", ondelete="RESTRICT"), nullable=False
    )
    release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_releases.id", ondelete="RESTRICT"), nullable=False
    )
    sensitivity: Mapped[str] = mapped_column(String(30), nullable=False)
    permission_resource_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="candidate")
    supersedes_link_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_links.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CompanyOntologyEvent(Base):
    __tablename__ = "company_ontology_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "release_id",
            "stable_event_key",
            name="uq_company_ontology_event_release_key",
        ),
        CheckConstraint(
            "status IN ('candidate','active','superseded','held','revoked')",
            name="ck_company_ontology_event_status",
        ),
        CheckConstraint(_SENSITIVITY_CHECK, name="ck_company_ontology_event_sensitivity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stable_event_key: Mapped[str] = mapped_column(String(500), nullable=False)
    event_type_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    subject_object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_objects.id", ondelete="RESTRICT"), nullable=True
    )
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sequence: Mapped[str | None] = mapped_column(String(300), nullable=True)
    evidence_bundle_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    curation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_curation_runs.id", ondelete="RESTRICT"), nullable=False
    )
    release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_releases.id", ondelete="RESTRICT"), nullable=False
    )
    sensitivity: Mapped[str] = mapped_column(String(30), nullable=False)
    permission_resource_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="candidate")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CompanyOntologyEvidenceBinding(Base):
    """Fact-to-evidence bundle membership and sufficient-support semantics."""

    __tablename__ = "company_ontology_evidence_bindings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "subject_kind",
            "subject_id",
            "bundle_key",
            "evidence_id",
            name="uq_company_ontology_evidence_binding",
        ),
        CheckConstraint(
            "support_mode IN ('joint','independent_sufficient')",
            name="ck_company_ontology_evidence_support_mode",
        ),
        CheckConstraint(
            "status IN ('active','held_for_authority_review','revoked')",
            name="ck_company_ontology_evidence_binding_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    bundle_key: Mapped[str] = mapped_column(String(500), nullable=False)
    support_mode: Mapped[str] = mapped_column(String(40), nullable=False)
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_knowledge_evidence.id", ondelete="RESTRICT"), nullable=False
    )
    source_acl_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CompanyOntologyReleaseItem(Base):
    """Immutable membership ledger for one published release."""

    __tablename__ = "company_ontology_release_items"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "release_id",
            "item_kind",
            "item_id",
            name="uq_company_ontology_release_item",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_ontology_releases.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    item_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    item_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
