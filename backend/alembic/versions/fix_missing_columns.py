"""Fix missing columns: llm_models.max_input_tokens + chat_sessions.summary.

These columns were defined in SQLAlchemy models but never had a migration.

Revision ID: fix_missing_columns_0328
Revises: simplify_quota_token_only_0327
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "fix_missing_columns_0328"
down_revision: Union[str, Sequence[str], None] = "simplify_quota_token_only_0327"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Already added via direct SQL; IF NOT EXISTS ensures idempotency
    op.execute("ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS max_input_tokens INTEGER")
    op.execute("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS summary VARCHAR")


def downgrade() -> None:
    op.drop_column("chat_sessions", "summary")
    op.drop_column("llm_models", "max_input_tokens")
