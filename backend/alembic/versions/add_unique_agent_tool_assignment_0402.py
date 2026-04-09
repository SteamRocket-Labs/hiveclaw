"""Add unique constraint for agent tool assignments.

Revision ID: add_unique_agent_tool_assignment_0402
Revises: add_agent_capability_installs_0402
Create Date: 2026-04-02
"""

from typing import Sequence, Union

from alembic import op


revision: str = "add_unique_agent_tool_assignment_0402"
down_revision: Union[str, None] = "add_agent_capability_installs_0402"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from sqlalchemy import text
    conn = op.get_bind()
    exists = conn.execute(
        text("SELECT 1 FROM pg_constraint WHERE conname = 'uq_agent_tools_agent_tool'")
    ).scalar()
    if exists:
        return

    op.execute(
        """
        DELETE FROM agent_tools
        WHERE id IN (
            SELECT id FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY agent_id, tool_id
                        ORDER BY created_at ASC NULLS FIRST, id ASC
                    ) AS rn
                FROM agent_tools
            ) dedupe
            WHERE dedupe.rn > 1
        )
        """
    )
    op.create_unique_constraint(
        "uq_agent_tools_agent_tool",
        "agent_tools",
        ["agent_id", "tool_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_agent_tools_agent_tool", "agent_tools", type_="unique")
