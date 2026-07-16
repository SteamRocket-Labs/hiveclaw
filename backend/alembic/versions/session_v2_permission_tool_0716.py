"""Make approval waiting and tool effect authority orthogonal.

Revision ID: session_v2_permission_tool_0716
Revises: session_v2_round_outcome_0716
"""

from __future__ import annotations

from alembic import op

from migration_snapshots.session_v2_permission_tool_contract_0716 import (
    DOWNGRADE_GUARD_SQL_STATEMENTS,
    DOWNGRADE_SQL_STATEMENTS,
    UPGRADE_SQL_STATEMENTS,
)


revision = "session_v2_permission_tool_0716"
down_revision = "session_v2_round_outcome_0716"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in UPGRADE_SQL_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in (*DOWNGRADE_GUARD_SQL_STATEMENTS, *DOWNGRADE_SQL_STATEMENTS):
        op.execute(statement)
