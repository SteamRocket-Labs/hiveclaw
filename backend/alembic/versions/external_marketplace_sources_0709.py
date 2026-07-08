"""Add external marketplace source discovery tables.

Revision ID: external_marketplace_sources_0709
Revises: external_extension_session_try_0709
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "external_marketplace_sources_0709"
down_revision = "external_extension_session_try_0709"
branch_labels = None
depends_on = None


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
        CREATE POLICY {policy_name} ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
        """
    )


def upgrade() -> None:
    op.create_table(
        "external_marketplace_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("source_type", sa.String(length=60), server_default="manual", nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="enabled", nullable=False),
        sa.Column("sync_status", sa.String(length=30), server_default="never_synced", nullable=False),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("config_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "source_uri", name="uq_external_marketplace_source_uri"),
    )
    op.create_index(op.f("ix_external_marketplace_sources_tenant_id"), "external_marketplace_sources", ["tenant_id"])
    op.create_index(op.f("ix_external_marketplace_sources_source_type"), "external_marketplace_sources", ["source_type"])
    op.create_index(op.f("ix_external_marketplace_sources_status"), "external_marketplace_sources", ["status"])
    op.create_index(op.f("ix_external_marketplace_sources_sync_status"), "external_marketplace_sources", ["sync_status"])
    op.create_index(
        op.f("ix_external_marketplace_sources_created_by_user_id"),
        "external_marketplace_sources",
        ["created_by_user_id"],
    )
    _enable_rls("external_marketplace_sources")

    op.create_table(
        "external_marketplace_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("external_marketplace_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_key", sa.String(length=260), nullable=False),
        sa.Column("display_name", sa.String(length=260), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_format", sa.String(length=60), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.String(length=300), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="available", nullable=False),
        sa.Column("manifest_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("compatibility_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column(
            "review_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("external_capability_reviews.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("external_capability_snapshots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "source_id", "external_key", name="uq_external_marketplace_entry_key"),
    )
    op.create_index(op.f("ix_external_marketplace_entries_tenant_id"), "external_marketplace_entries", ["tenant_id"])
    op.create_index(op.f("ix_external_marketplace_entries_source_id"), "external_marketplace_entries", ["source_id"])
    op.create_index(op.f("ix_external_marketplace_entries_source_format"), "external_marketplace_entries", ["source_format"])
    op.create_index(op.f("ix_external_marketplace_entries_status"), "external_marketplace_entries", ["status"])
    op.create_index(op.f("ix_external_marketplace_entries_review_id"), "external_marketplace_entries", ["review_id"])
    op.create_index(op.f("ix_external_marketplace_entries_snapshot_id"), "external_marketplace_entries", ["snapshot_id"])
    _enable_rls("external_marketplace_entries")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_external_marketplace_entries ON external_marketplace_entries")
    op.execute("ALTER TABLE external_marketplace_entries DISABLE ROW LEVEL SECURITY")
    op.drop_table("external_marketplace_entries")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_external_marketplace_sources ON external_marketplace_sources")
    op.execute("ALTER TABLE external_marketplace_sources DISABLE ROW LEVEL SECURITY")
    op.drop_table("external_marketplace_sources")
