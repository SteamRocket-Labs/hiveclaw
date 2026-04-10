"""Add wechat_personal to channel_type_enum.

Revision ID: add_wechat_personal_channel_0411
Revises: add_llm_provider_configs_0410
Create Date: 2026-04-11
"""

from alembic import op

revision = "add_wechat_personal_channel_0411"
down_revision = "add_agent_smart_model_routing_0410"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE channel_type_enum ADD VALUE IF NOT EXISTS 'wechat_personal'")


def downgrade() -> None:
    # PostgreSQL doesn't support removing enum values directly
    pass
