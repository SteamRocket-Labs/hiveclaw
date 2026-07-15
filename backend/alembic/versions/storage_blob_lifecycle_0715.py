"""Add tenant-scoped immutable blob/ref/GC lifecycle authority.

Revision ID: storage_blob_lifecycle_0715
Revises: agent_model_tenant_authority_0715
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "storage_blob_lifecycle_0715"
down_revision = "agent_model_tenant_authority_0715"
branch_labels = None
depends_on = None

_TABLES = ("storage_blobs", "storage_blob_refs", "storage_gc_runs")


def _force_strict_tenant_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{table} ON {table}
        USING (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        WITH CHECK (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        """
    )


def upgrade() -> None:
    op.create_table(
        "storage_blobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_type", sa.String(length=20), server_default="tenant", nullable=False),
        sa.Column("kind", sa.String(length=60), nullable=False),
        sa.Column("retention_class", sa.String(length=40), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("bucket", sa.String(length=200), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=200), nullable=True),
        sa.Column("encryption_key_id", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("scope_type IN ('tenant','system')", name="ck_storage_blobs_scope_type"),
        sa.CheckConstraint(
            "state IN ('uploading','available','quarantined','deleting','deleted','failed')",
            name="ck_storage_blobs_state",
        ),
        sa.CheckConstraint("char_length(content_sha256) = 64", name="ck_storage_blobs_sha256_length"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_storage_blobs_size_nonnegative"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_storage_blobs_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "content_sha256", name="uq_storage_blobs_tenant_digest"),
        sa.UniqueConstraint("provider", "bucket", "object_key", name="uq_storage_blobs_provider_location"),
    )
    op.create_index("ix_storage_blobs_tenant_id", "storage_blobs", ["tenant_id"])
    op.create_index("ix_storage_blobs_content_sha256", "storage_blobs", ["content_sha256"])
    op.create_index(
        "ix_storage_blobs_tenant_state_delete_after",
        "storage_blobs",
        ["tenant_id", "state", "delete_after"],
    )

    op.create_table(
        "storage_blob_refs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("blob_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_type", sa.String(length=60), nullable=False),
        sa.Column("owner_id", sa.String(length=300), nullable=False),
        sa.Column("purpose", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("pinned_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("legal_hold", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "blob_id"],
            ["storage_blobs.tenant_id", "storage_blobs.id"],
            ondelete="CASCADE",
            name="fk_storage_blob_refs_tenant_blob",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "blob_id",
            "owner_type",
            "owner_id",
            "purpose",
            name="uq_storage_blob_refs_owner_purpose",
        ),
    )
    op.create_index("ix_storage_blob_refs_tenant_id", "storage_blob_refs", ["tenant_id"])
    op.create_index("ix_storage_blob_refs_blob_id", "storage_blob_refs", ["blob_id"])
    op.create_index(
        "ix_storage_blob_refs_tenant_owner",
        "storage_blob_refs",
        ["tenant_id", "owner_type", "owner_id"],
    )

    op.create_table(
        "storage_gc_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("mode", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("candidate_count", sa.BigInteger(), nullable=False),
        sa.Column("candidate_bytes", sa.BigInteger(), nullable=False),
        sa.Column("processed_count", sa.BigInteger(), nullable=False),
        sa.Column("processed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("failed_count", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "mode IN ('inventory','backfill','dry_run','quarantine','sweep','restore')",
            name="ck_storage_gc_runs_mode",
        ),
        sa.CheckConstraint(
            "status IN ('running','completed','partial','failed')",
            name="ck_storage_gc_runs_status",
        ),
        sa.CheckConstraint("char_length(manifest_sha256) = 64", name="ck_storage_gc_runs_sha256_length"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "manifest_sha256", "mode", name="uq_storage_gc_runs_manifest_mode"),
    )
    op.create_index("ix_storage_gc_runs_tenant_id", "storage_gc_runs", ["tenant_id"])
    op.create_index("ix_storage_gc_runs_tenant_started", "storage_gc_runs", ["tenant_id", "started_at"])

    for table in _TABLES:
        _force_strict_tenant_rls(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table)
