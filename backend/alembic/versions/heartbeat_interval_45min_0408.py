"""Reduce heartbeat interval default from 120 to 45 minutes.

Matches architecture design: T2→T3 curation should run every 45 minutes.

Revision ID: heartbeat_interval_45min_0408
Revises: add_trigger_reply_context_0407
Create Date: 2026-04-08
"""

from typing import Union

from alembic import op

revision: str = "heartbeat_interval_45min_0408"
down_revision: Union[str, None] = "add_trigger_reply_context_0407"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    # Update existing agents still on the old 120-minute default
    op.execute(
        "UPDATE agents SET heartbeat_interval_minutes = 45 "
        "WHERE heartbeat_interval_minutes = 120"
    )
    # Update tenant floor to match
    op.execute(
        "UPDATE tenants SET min_heartbeat_interval_minutes = 45 "
        "WHERE min_heartbeat_interval_minutes = 120"
    )
    # Change column defaults for new records
    op.alter_column("agents", "heartbeat_interval_minutes", server_default="45")
    op.alter_column("tenants", "min_heartbeat_interval_minutes", server_default="45")


def downgrade() -> None:
    op.execute(
        "UPDATE agents SET heartbeat_interval_minutes = 120 "
        "WHERE heartbeat_interval_minutes = 45"
    )
    op.execute(
        "UPDATE tenants SET min_heartbeat_interval_minutes = 120 "
        "WHERE min_heartbeat_interval_minutes = 45"
    )
    op.alter_column("agents", "heartbeat_interval_minutes", server_default="120")
    op.alter_column("tenants", "min_heartbeat_interval_minutes", server_default="120")
