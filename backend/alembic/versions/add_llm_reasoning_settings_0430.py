"""Add reasoning settings to llm_models.

Revision ID: add_llm_reasoning_settings_0430
Revises: add_tool_runtime_activity_enum_0428
"""

from alembic import op


revision = "add_llm_reasoning_settings_0430"
down_revision = "add_tool_runtime_activity_enum_0428"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS temperature DOUBLE PRECISION")
    op.execute("ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS reasoning_mode VARCHAR(32)")
    op.execute("ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS reasoning_effort VARCHAR(32)")
    op.execute("ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS reasoning_budget_tokens INTEGER")
    op.execute("ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS reasoning_display VARCHAR(32)")
    op.execute("ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS preserve_reasoning BOOLEAN")
    op.execute("ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS text_verbosity VARCHAR(32)")
    op.execute("ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS provider_options JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE llm_models DROP COLUMN IF EXISTS provider_options")
    op.execute("ALTER TABLE llm_models DROP COLUMN IF EXISTS text_verbosity")
    op.execute("ALTER TABLE llm_models DROP COLUMN IF EXISTS preserve_reasoning")
    op.execute("ALTER TABLE llm_models DROP COLUMN IF EXISTS reasoning_display")
    op.execute("ALTER TABLE llm_models DROP COLUMN IF EXISTS reasoning_budget_tokens")
    op.execute("ALTER TABLE llm_models DROP COLUMN IF EXISTS reasoning_effort")
    op.execute("ALTER TABLE llm_models DROP COLUMN IF EXISTS reasoning_mode")
    op.execute("ALTER TABLE llm_models DROP COLUMN IF EXISTS temperature")
