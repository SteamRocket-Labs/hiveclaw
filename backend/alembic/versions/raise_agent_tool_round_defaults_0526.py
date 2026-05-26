"""Raise legacy agent tool-round defaults.

Revision ID: raise_agent_tool_round_defaults_0526
Revises: coordination_charter_0522
Create Date: 2026-05-26
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "raise_agent_tool_round_defaults_0526"
down_revision: Union[str, None] = "coordination_charter_0522"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE agents SET max_tool_rounds = 200 "
        "WHERE max_tool_rounds IS NULL OR max_tool_rounds = 50"
    )
    op.alter_column(
        "agents",
        "max_tool_rounds",
        existing_type=sa.Integer(),
        nullable=False,
        server_default="200",
    )


def downgrade() -> None:
    op.alter_column(
        "agents",
        "max_tool_rounds",
        existing_type=sa.Integer(),
        nullable=False,
        server_default="50",
    )
