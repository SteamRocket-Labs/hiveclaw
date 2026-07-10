"""Converge mutable agent ownership on owner_user_id.

Revision ID: agent_authority_0710
Revises: hr_creation_drafts_0710
"""

from alembic import op


revision = "agent_authority_0710"
down_revision = "hr_creation_drafts_0710"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # creator_id remains immutable provenance. Legacy rows inherit sponsor
    # first, then creator, as their current owner.
    op.execute(
        """
        UPDATE agents
        SET owner_user_id = COALESCE(sponsor_user_id, creator_id)
        WHERE owner_user_id IS NULL
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agents_owner_tenant_active "
        "ON agents (owner_user_id, tenant_id, deleted_at)"
    )


def downgrade() -> None:
    # Retain owner data; rewriting it into creator provenance is not reversible.
    op.execute("DROP INDEX IF EXISTS ix_agents_owner_tenant_active")
