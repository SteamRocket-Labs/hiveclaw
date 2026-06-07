"""Add always-load flag to MCP server assignments.

Revision ID: mcp_assignment_always_load_0607
Revises: subagent_evolution_auto_0605
Create Date: 2026-06-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "mcp_assignment_always_load_0607"
down_revision: Union[str, None] = "subagent_evolution_auto_0605"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists() -> bool:
    from sqlalchemy import text

    conn = op.get_bind()
    result = conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'agent_mcp_server_assignments' AND column_name = 'always_load'"
        )
    )
    return result.scalar() is not None


def upgrade() -> None:
    if _column_exists():
        return
    op.add_column(
        "agent_mcp_server_assignments",
        sa.Column("always_load", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    if _column_exists():
        op.drop_column("agent_mcp_server_assignments", "always_load")
