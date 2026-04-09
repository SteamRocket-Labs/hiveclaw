"""tool_name_tenant_unique_constraint

Revision ID: 82dbade25f85
Revises: remove_agent_kind_0328
Create Date: 2026-03-30 19:15:44.874432

Fixes: MCP tools created without tenant_id leak across all tenants in
enterprise tools page. This migration:
1. Drops the global unique on tools.name
2. Adds composite unique on (name, tenant_id)
3. Backfills tenant_id on existing MCP tools from AgentTool → Agent
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '82dbade25f85'
down_revision: Union[str, None] = 'remove_agent_kind_0328'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Backfill tenant_id on MCP tools that have agent assignments
    op.execute("""
        UPDATE tools
        SET tenant_id = sub.agent_tenant_id
        FROM (
            SELECT DISTINCT ON (at.tool_id)
                at.tool_id,
                a.tenant_id AS agent_tenant_id
            FROM agent_tools at
            JOIN agents a ON a.id = at.agent_id
            WHERE a.tenant_id IS NOT NULL
            ORDER BY at.tool_id, at.created_at ASC
        ) sub
        WHERE tools.id = sub.tool_id
          AND tools.type = 'mcp'
          AND tools.tenant_id IS NULL
    """)

    # 2. Drop old global unique on name
    op.drop_constraint('tools_name_key', 'tools', type_='unique')

    # 3. Add composite unique (name, tenant_id)
    op.create_unique_constraint('uq_tools_name_tenant', 'tools', ['name', 'tenant_id'])


def downgrade() -> None:
    op.drop_constraint('uq_tools_name_tenant', 'tools', type_='unique')
    op.create_unique_constraint('tools_name_key', 'tools', ['name'])
