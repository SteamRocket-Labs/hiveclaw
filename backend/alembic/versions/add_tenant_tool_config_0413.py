"""Add tenant_tool_configs table for per-tenant tool configuration isolation.

Revision ID: add_tenant_tool_config_0413
Revises: add_pending_reply_context_0413
Create Date: 2026-04-13
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "add_tenant_tool_config_0413"
down_revision = "add_pending_reply_context_0413"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_tool_configs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("tool_id", UUID(as_uuid=True), sa.ForeignKey("tools.id", ondelete="CASCADE"), nullable=False),
        sa.Column("config", JSONB, server_default="{}"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "tool_id", name="uq_tenant_tool_config"),
    )


def downgrade() -> None:
    op.drop_table("tenant_tool_configs")
