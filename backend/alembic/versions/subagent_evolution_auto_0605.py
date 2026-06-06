"""Agent-level approval mode switch for the subagent evolution loop.

docs/subagent-evolution-loop.md §4.3: definition-improvement proposals are
approved manually by default; the owner may opt an agent into auto-approval
(same validation/audit path, only the human click is skipped).

Revision ID: subagent_evolution_auto_0605
Revises: coordination_rls_0604
Create Date: 2026-06-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "subagent_evolution_auto_0605"
down_revision: Union[str, None] = "coordination_rls_0604"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists() -> bool:
    from sqlalchemy import text

    conn = op.get_bind()
    result = conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'agents' AND column_name = 'subagent_evolution_auto_approve'"
        )
    )
    return result.scalar() is not None


def upgrade() -> None:
    if _column_exists():
        return  # create_all on a fresh deployment may have added it already
    op.add_column(
        "agents",
        sa.Column(
            "subagent_evolution_auto_approve",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    if _column_exists():
        op.drop_column("agents", "subagent_evolution_auto_approve")
