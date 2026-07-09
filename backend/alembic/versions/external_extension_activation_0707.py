"""Add external extension activations.

Revision ID: external_extension_activation_0707
Revises: external_capability_trust_gate_0707
Create Date: 2026-07-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "external_extension_activation_0707"
down_revision = "external_capability_trust_gate_0707"
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
        "external_extension_activations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("external_capability_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=30), server_default="active", nullable=False),
        sa.Column(
            "component_types_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "activation_result_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "activated_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "agent_id", "snapshot_id", name="uq_external_extension_activation"),
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_activations_tenant_id"),
        "external_extension_activations",
        ["tenant_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_activations_agent_id"),
        "external_extension_activations",
        ["agent_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_activations_snapshot_id"),
        "external_extension_activations",
        ["snapshot_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_activations_status"),
        "external_extension_activations",
        ["status"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_activations_activated_by_user_id"),
        "external_extension_activations",
        ["activated_by_user_id"],
        if_not_exists=True,
    )
    _enable_rls("external_extension_activations")


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_external_extension_activations ON external_extension_activations"
    )
    op.execute("ALTER TABLE external_extension_activations DISABLE ROW LEVEL SECURITY")
    op.drop_table("external_extension_activations")
