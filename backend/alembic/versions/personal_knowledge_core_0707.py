"""Add Personal Knowledge core records.

Revision ID: personal_knowledge_core_0707
Revises: external_extension_catalog_entries_0707
Create Date: 2026-07-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "personal_knowledge_core_0707"
down_revision = "external_extension_catalog_entries_0707"
branch_labels = None
depends_on = None


_KNOWLEDGE_TABLES = [
    "knowledge_documents",
    "knowledge_segments",
    "knowledge_entities",
    "knowledge_assertions",
    "knowledge_links",
    "knowledge_index_jobs",
    "knowledge_grants",
]


def _tenant_predicate(table: str) -> str:
    return f"""
        current_setting('app.current_tenant_id', true) = 'BYPASS'
        OR {table}.tenant_id::text = current_setting('app.current_tenant_id', true)
    """


def _enable_rls(table: str) -> None:
    predicate = _tenant_predicate(table)
    policy_name = f"tenant_isolation_{table}"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table}")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{table} ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
        """
    )


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("scope_type", sa.String(length=30), server_default="person", nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_kind", sa.String(length=40), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_hash", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=40), server_default="pending", nullable=False),
        sa.Column("sensitivity", sa.String(length=30), server_default="internal", nullable=False),
        sa.Column("agent_searchable", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("canonical_md_path", sa.Text(), nullable=False),
        sa.Column("canonical_md_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "doc_metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "scope_type", "scope_id", "source_sha256", name="uq_knowledge_document_source"
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_knowledge_documents_tenant_scope",
        "knowledge_documents",
        ["tenant_id", "scope_type", "scope_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_documents_tenant_id"),
        "knowledge_documents",
        ["tenant_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_documents_scope_type"),
        "knowledge_documents",
        ["scope_type"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_documents_scope_id"),
        "knowledge_documents",
        ["scope_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_documents_owner_user_id"),
        "knowledge_documents",
        ["owner_user_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_documents_source_kind"),
        "knowledge_documents",
        ["source_kind"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_documents_source_sha256"),
        "knowledge_documents",
        ["source_sha256"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_documents_artifact_hash"),
        "knowledge_documents",
        ["artifact_hash"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_documents_status"),
        "knowledge_documents",
        ["status"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_documents_sensitivity"),
        "knowledge_documents",
        ["sensitivity"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_documents_agent_searchable"),
        "knowledge_documents",
        ["agent_searchable"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_documents_created_by_user_id"),
        "knowledge_documents",
        ["created_by_user_id"],
        if_not_exists=True,
    )

    op.create_table(
        "knowledge_segments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope_type", sa.String(length=30), server_default="person", nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("segment_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "heading_path_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tsv", postgresql.TSVECTOR(), nullable=True),
        sa.Column(
            "segment_metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "document_id", "position", name="uq_knowledge_segment_position"),
        sa.UniqueConstraint("tenant_id", "document_id", "segment_hash", name="uq_knowledge_segment_hash"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_knowledge_segments_tenant_scope",
        "knowledge_segments",
        ["tenant_id", "scope_type", "scope_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_segments_tenant_id"),
        "knowledge_segments",
        ["tenant_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_segments_document_id"),
        "knowledge_segments",
        ["document_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_segments_scope_type"),
        "knowledge_segments",
        ["scope_type"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_segments_scope_id"),
        "knowledge_segments",
        ["scope_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_segments_segment_hash"),
        "knowledge_segments",
        ["segment_hash"],
        if_not_exists=True,
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_knowledge_segments_tsv_gin ON knowledge_segments USING gin (tsv)")

    op.create_table(
        "knowledge_entities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("scope_type", sa.String(length=30), server_default="person", nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canonical_name", sa.String(length=300), nullable=False),
        sa.Column("entity_type", sa.String(length=80), server_default="freeform", nullable=False),
        sa.Column(
            "aliases_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), server_default="1.0", nullable=False),
        sa.Column(
            "merged_into_entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_entities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_refs_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "scope_type", "scope_id", "entity_type", "canonical_name", name="uq_knowledge_entity_name"
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_knowledge_entities_tenant_scope",
        "knowledge_entities",
        ["tenant_id", "scope_type", "scope_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_entities_tenant_id"),
        "knowledge_entities",
        ["tenant_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_entities_scope_type"),
        "knowledge_entities",
        ["scope_type"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_entities_scope_id"),
        "knowledge_entities",
        ["scope_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_entities_canonical_name"),
        "knowledge_entities",
        ["canonical_name"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_entities_entity_type"),
        "knowledge_entities",
        ["entity_type"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_entities_merged_into_entity_id"),
        "knowledge_entities",
        ["merged_into_entity_id"],
        if_not_exists=True,
    )

    op.create_table(
        "knowledge_assertions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("scope_type", sa.String(length=30), server_default="person", nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "source_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "subject_entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_entities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("subject_text", sa.String(length=300), nullable=False),
        sa.Column("predicate", sa.String(length=120), nullable=False),
        sa.Column(
            "object_entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_entities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("object_text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("status", sa.String(length=40), server_default="active", nullable=False),
        sa.Column(
            "source_refs_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "scope_type",
            "scope_id",
            "subject_text",
            "predicate",
            "object_text",
            name="uq_knowledge_assertion_text",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_knowledge_assertions_tenant_scope",
        "knowledge_assertions",
        ["tenant_id", "scope_type", "scope_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_assertions_tenant_id"),
        "knowledge_assertions",
        ["tenant_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_assertions_scope_type"),
        "knowledge_assertions",
        ["scope_type"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_assertions_scope_id"),
        "knowledge_assertions",
        ["scope_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_assertions_source_document_id"),
        "knowledge_assertions",
        ["source_document_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_assertions_subject_entity_id"),
        "knowledge_assertions",
        ["subject_entity_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_assertions_predicate"),
        "knowledge_assertions",
        ["predicate"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_assertions_object_entity_id"),
        "knowledge_assertions",
        ["object_entity_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_assertions_status"),
        "knowledge_assertions",
        ["status"],
        if_not_exists=True,
    )

    op.create_table(
        "knowledge_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("scope_type", sa.String(length=30), server_default="person", nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_kind", sa.String(length=40), nullable=False),
        sa.Column("from_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_kind", sa.String(length=40), nullable=False),
        sa.Column("to_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relation", sa.String(length=80), nullable=False),
        sa.Column("confidence", sa.Float(), server_default="1.0", nullable=False),
        sa.Column(
            "source_refs_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
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
        if_not_exists=True,
    )
    op.create_index(
        "ix_knowledge_links_tenant_scope",
        "knowledge_links",
        ["tenant_id", "scope_type", "scope_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_links_tenant_id"),
        "knowledge_links",
        ["tenant_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_links_scope_type"),
        "knowledge_links",
        ["scope_type"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_links_scope_id"),
        "knowledge_links",
        ["scope_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_links_from_id"),
        "knowledge_links",
        ["from_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_links_to_id"),
        "knowledge_links",
        ["to_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_links_relation"),
        "knowledge_links",
        ["relation"],
        if_not_exists=True,
    )

    op.create_table(
        "knowledge_index_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope_type", sa.String(length=30), server_default="person", nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_hash", sa.String(length=128), nullable=False),
        sa.Column("stage", sa.String(length=50), server_default="queued", nullable=False),
        sa.Column("status", sa.String(length=40), server_default="queued", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "job_metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "document_id", "artifact_hash", name="uq_knowledge_index_job_artifact"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_knowledge_index_jobs_tenant_scope",
        "knowledge_index_jobs",
        ["tenant_id", "scope_type", "scope_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_index_jobs_tenant_id"),
        "knowledge_index_jobs",
        ["tenant_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_index_jobs_document_id"),
        "knowledge_index_jobs",
        ["document_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_index_jobs_scope_type"),
        "knowledge_index_jobs",
        ["scope_type"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_index_jobs_scope_id"),
        "knowledge_index_jobs",
        ["scope_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_index_jobs_artifact_hash"),
        "knowledge_index_jobs",
        ["artifact_hash"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_index_jobs_stage"),
        "knowledge_index_jobs",
        ["stage"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_index_jobs_status"),
        "knowledge_index_jobs",
        ["status"],
        if_not_exists=True,
    )

    op.create_table(
        "knowledge_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("scope_type", sa.String(length=30), server_default="person", nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_type", sa.String(length=30), server_default="scope", nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("grantee_type", sa.String(length=30), nullable=False),
        sa.Column("grantee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission", sa.String(length=30), server_default="search", nullable=False),
        sa.Column(
            "grant_metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
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
        if_not_exists=True,
    )
    op.create_index(
        "ix_knowledge_grants_tenant_scope",
        "knowledge_grants",
        ["tenant_id", "scope_type", "scope_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_grants_tenant_id"),
        "knowledge_grants",
        ["tenant_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_grants_scope_type"),
        "knowledge_grants",
        ["scope_type"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_grants_scope_id"),
        "knowledge_grants",
        ["scope_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_grants_resource_type"),
        "knowledge_grants",
        ["resource_type"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_grants_resource_id"),
        "knowledge_grants",
        ["resource_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_grants_document_id"),
        "knowledge_grants",
        ["document_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_grants_grantee_type"),
        "knowledge_grants",
        ["grantee_type"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_grants_grantee_id"),
        "knowledge_grants",
        ["grantee_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_grants_permission"),
        "knowledge_grants",
        ["permission"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_knowledge_grants_created_by_user_id"),
        "knowledge_grants",
        ["created_by_user_id"],
        if_not_exists=True,
    )

    for table in _KNOWLEDGE_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in reversed(_KNOWLEDGE_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.drop_table(table)
