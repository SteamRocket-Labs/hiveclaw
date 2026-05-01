"""Add unique system HR agent guard per tenant.

Revision ID: add_unique_system_hr_agent_per_tenant_0501
Revises: add_llm_reasoning_settings_0430
"""

from alembic import op


revision = "add_unique_system_hr_agent_per_tenant_0501"
down_revision = "add_llm_reasoning_settings_0430"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_one_system_hr_per_tenant "
        "ON agents (tenant_id) "
        "WHERE tenant_id IS NOT NULL "
        "AND agent_class = 'internal_system' "
        "AND name = '__system_hr__'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_agents_one_system_hr_per_tenant")
