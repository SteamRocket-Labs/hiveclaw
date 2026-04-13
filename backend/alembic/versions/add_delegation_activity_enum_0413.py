"""Add delegation action types to activity_action_enum.

Revision ID: add_delegation_activity_enum_0413
Revises: dedup_identity_providers_0413
Create Date: 2026-04-13
"""

from alembic import op

revision = "add_delegation_activity_enum_0413"
down_revision = "dedup_identity_providers_0413"
branch_labels = None
depends_on = None

_NEW_VALUES = [
    "delegation_started",
    "delegation_completed",
    "delegation_failed",
    "delegation_killed",
    "delegation_error",
]


def upgrade() -> None:
    for value in _NEW_VALUES:
        op.execute(f"ALTER TYPE activity_action_enum ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # PostgreSQL does not support DROP VALUE from enum types.
    # The 5 delegation_* values remain in activity_action_enum.
    # This is safe: the old code simply never writes those values.
    import logging

    logging.getLogger(__name__).warning(
        "Downgrade of add_delegation_activity_enum_0413: "
        "delegation_* enum values cannot be removed from PostgreSQL. "
        "They remain in activity_action_enum but will not be written by the downgraded code."
    )
