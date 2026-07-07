"""Add external extension catalog entries.

Revision ID: external_extension_catalog_entries_0707
Revises: external_extension_activation_0707
Create Date: 2026-07-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "external_extension_catalog_entries_0707"
down_revision = "external_extension_activation_0707"
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
        "external_extension_catalog_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("external_capability_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("component_type", sa.String(length=40), nullable=False),
        sa.Column("component_name", sa.String(length=200), nullable=False),
        sa.Column("qualified_name", sa.String(length=300), nullable=False),
        sa.Column("policy", sa.String(length=40), server_default="optional", nullable=False),
        sa.Column("status", sa.String(length=30), server_default="available", nullable=False),
        sa.Column("source_format", sa.String(length=60), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "snapshot_id", "qualified_name", name="uq_external_catalog_snapshot_component"),
    )
    op.create_index(op.f("ix_external_extension_catalog_entries_tenant_id"), "external_extension_catalog_entries", ["tenant_id"])
    op.create_index(op.f("ix_external_extension_catalog_entries_snapshot_id"), "external_extension_catalog_entries", ["snapshot_id"])
    op.create_index(
        op.f("ix_external_extension_catalog_entries_component_type"),
        "external_extension_catalog_entries",
        ["component_type"],
    )
    op.create_index(
        op.f("ix_external_extension_catalog_entries_component_name"),
        "external_extension_catalog_entries",
        ["component_name"],
    )
    op.create_index(op.f("ix_external_extension_catalog_entries_policy"), "external_extension_catalog_entries", ["policy"])
    op.create_index(op.f("ix_external_extension_catalog_entries_status"), "external_extension_catalog_entries", ["status"])
    op.create_index(
        op.f("ix_external_extension_catalog_entries_source_format"),
        "external_extension_catalog_entries",
        ["source_format"],
    )
    _enable_rls("external_extension_catalog_entries")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_external_extension_catalog_entries ON external_extension_catalog_entries")
    op.execute("ALTER TABLE external_extension_catalog_entries DISABLE ROW LEVEL SECURITY")
    op.drop_table("external_extension_catalog_entries")
