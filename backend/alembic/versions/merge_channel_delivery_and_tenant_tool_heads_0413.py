"""Merge channel delivery and tenant tool config migration heads.

Revision ID: merge_channel_delivery_and_tenant_tool_heads_0413
Revises: add_tenant_tool_config_0413, add_channel_delivery_targets_0413
Create Date: 2026-04-13
"""

revision = "merge_channel_delivery_and_tenant_tool_heads_0413"
down_revision = ("add_tenant_tool_config_0413", "add_channel_delivery_targets_0413")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
