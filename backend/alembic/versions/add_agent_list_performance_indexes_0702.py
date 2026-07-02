"""Add indexes for hot agent list queries.

Revision ID: add_agent_list_performance_indexes_0702
Revises: widen_runtime_coordination_identifiers_0702
Create Date: 2026-07-02
"""

from __future__ import annotations

from alembic import op


revision = "add_agent_list_performance_indexes_0702"
down_revision = "widen_runtime_coordination_identifiers_0702"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_agents_tenant_active_created_at",
        "agents",
        ["tenant_id", "deleted_at", "deactivated_at", "agent_class", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_agents_creator_tenant_active_created_at",
        "agents",
        ["creator_id", "tenant_id", "deleted_at", "deactivated_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_permissions_scope_lookup",
        "agent_permissions",
        ["scope_type", "scope_id", "agent_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_permissions_scope_lookup", table_name="agent_permissions")
    op.drop_index("ix_agents_creator_tenant_active_created_at", table_name="agents")
    op.drop_index("ix_agents_tenant_active_created_at", table_name="agents")
