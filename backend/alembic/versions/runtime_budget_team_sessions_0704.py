"""Add runtime budget team session counters.

Revision ID: runtime_budget_team_sessions_0704
Revises: runtime_budget_control_plane_0704
Create Date: 2026-07-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "runtime_budget_team_sessions_0704"
down_revision = "runtime_budget_control_plane_0704"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runtime_budget_policies",
        sa.Column("max_team_sessions", sa.Integer(), nullable=True),
        if_not_exists=True,
    )
    op.add_column(
        "runtime_budget_runs",
        sa.Column("max_team_sessions", sa.Integer(), nullable=True),
        if_not_exists=True,
    )
    op.add_column(
        "runtime_budget_runs",
        sa.Column("reserved_team_sessions", sa.Integer(), server_default="0", nullable=False),
        if_not_exists=True,
    )
    op.add_column(
        "runtime_budget_runs",
        sa.Column("used_team_sessions", sa.Integer(), server_default="0", nullable=False),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_column("runtime_budget_runs", "used_team_sessions")
    op.drop_column("runtime_budget_runs", "reserved_team_sessions")
    op.drop_column("runtime_budget_runs", "max_team_sessions")
    op.drop_column("runtime_budget_policies", "max_team_sessions")
