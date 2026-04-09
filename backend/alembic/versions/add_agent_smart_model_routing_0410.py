"""Add smart model routing config to agents.

Revision ID: add_agent_smart_model_routing_0410
Revises: feishu_identity_provider_0410
Create Date: 2026-04-10
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "add_agent_smart_model_routing_0410"
down_revision: Union[str, None] = "feishu_identity_provider_0410"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("smart_model_routing", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agents", "smart_model_routing")
