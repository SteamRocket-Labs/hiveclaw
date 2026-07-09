"""Add external extension component and hook evidence.

Revision ID: external_extension_components_0709
Revises: personal_knowledge_core_0707
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "external_extension_components_0709"
down_revision = "personal_knowledge_core_0707"
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
    op.add_column(
        "external_extension_activations",
        sa.Column(
            "selected_components_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        if_not_exists=True,
    )
    op.add_column(
        "external_extension_activations",
        sa.Column(
            "credential_handles_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        if_not_exists=True,
    )

    op.create_table(
        "external_extension_components",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("external_capability_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("component_type", sa.String(length=40), nullable=False),
        sa.Column("component_name", sa.String(length=200), nullable=False),
        sa.Column("qualified_name", sa.String(length=300), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("content_sha256", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="approved", nullable=False),
        sa.Column(
            "runtime_projection_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "snapshot_id", "qualified_name", name="uq_external_component_snapshot_name"),
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_components_tenant_id"),
        "external_extension_components",
        ["tenant_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_components_snapshot_id"),
        "external_extension_components",
        ["snapshot_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_components_component_type"),
        "external_extension_components",
        ["component_type"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_components_component_name"),
        "external_extension_components",
        ["component_name"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_components_content_sha256"),
        "external_extension_components",
        ["content_sha256"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_components_status"),
        "external_extension_components",
        ["status"],
        if_not_exists=True,
    )
    _enable_rls("external_extension_components")

    op.create_table(
        "external_extension_hook_registrations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("external_capability_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "component_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("external_extension_components.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("qualified_name", sa.String(length=300), nullable=False),
        sa.Column("event", sa.String(length=80), nullable=False),
        sa.Column("handler", sa.String(length=200), nullable=False),
        sa.Column("mode", sa.String(length=30), server_default="observe", nullable=False),
        sa.Column(
            "matcher_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("approval_required", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="pending_approval", nullable=False),
        sa.Column(
            "approval_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "snapshot_id", "qualified_name", name="uq_external_hook_snapshot_name"),
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_hook_registrations_tenant_id"),
        "external_extension_hook_registrations",
        ["tenant_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_hook_registrations_snapshot_id"),
        "external_extension_hook_registrations",
        ["snapshot_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_hook_registrations_component_id"),
        "external_extension_hook_registrations",
        ["component_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_hook_registrations_event"),
        "external_extension_hook_registrations",
        ["event"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_hook_registrations_status"),
        "external_extension_hook_registrations",
        ["status"],
        if_not_exists=True,
    )
    _enable_rls("external_extension_hook_registrations")


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_external_extension_hook_registrations "
        "ON external_extension_hook_registrations"
    )
    op.execute("ALTER TABLE external_extension_hook_registrations DISABLE ROW LEVEL SECURITY")
    op.drop_table("external_extension_hook_registrations")

    op.execute("DROP POLICY IF EXISTS tenant_isolation_external_extension_components ON external_extension_components")
    op.execute("ALTER TABLE external_extension_components DISABLE ROW LEVEL SECURITY")
    op.drop_table("external_extension_components")

    op.drop_column("external_extension_activations", "credential_handles_json")
    op.drop_column("external_extension_activations", "selected_components_json")
