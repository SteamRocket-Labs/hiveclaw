"""Add max_output_tokens column to llm_models table.

Revision ID: add_llm_max_output_tokens
Revises: add_skill_tenant_id
"""
from alembic import op
import sqlalchemy as sa

revision = "add_llm_max_output_tokens"
down_revision = "add_skill_tenant_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS max_output_tokens INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE llm_models DROP COLUMN IF EXISTS max_output_tokens")
