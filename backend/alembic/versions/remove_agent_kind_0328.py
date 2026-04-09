"""Remove agent_kind column — no more main/sub agent distinction.

Revision ID: remove_agent_kind_0328
Revises: fix_missing_columns_0328
"""

from typing import Sequence, Union

from alembic import op


revision: str = "remove_agent_kind_0328"
down_revision: Union[str, Sequence[str], None] = "fix_missing_columns_0328"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS agent_kind")


def downgrade() -> None:
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS agent_kind VARCHAR(10) NOT NULL DEFAULT 'main'")
