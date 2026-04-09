"""Add agent_class and security_zone columns for internal/external separation.

Revision ID: add_agent_classification
Revises: add_feature_flags
"""
from alembic import op

revision = "add_agent_classification"
down_revision = "add_feature_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # agent_class: what the agent IS (platform role)
    # Values: internal_system, internal_tenant, external_gateway, external_api
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS agent_class VARCHAR(20) NOT NULL DEFAULT 'internal_tenant'"
    )
    # security_zone: how the agent is SECURED (data access policy)
    # Values: standard, restricted, public
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS security_zone VARCHAR(20) NOT NULL DEFAULT 'standard'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS agent_class")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS security_zone")
