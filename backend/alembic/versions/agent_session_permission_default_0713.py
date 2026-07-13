"""Persist the default session permission mode per agent.

Revision ID: agent_session_permission_default_0713
Revises: hr_draft_recovery_0712
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "agent_session_permission_default_0713"
down_revision = "hr_draft_recovery_0712"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "default_session_permission_mode",
            sa.String(length=32),
            server_default="default",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_agents_default_session_permission_mode",
        "agents",
        "default_session_permission_mode IN ('default', 'auto', 'bypassPermissions')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agents_default_session_permission_mode", "agents", type_="check")
    op.drop_column("agents", "default_session_permission_mode")
