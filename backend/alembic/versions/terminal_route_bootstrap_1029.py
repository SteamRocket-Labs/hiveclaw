"""Repair terminal routes on databases previously bootstrapped to head."""

from alembic import context
from alembic.script import ScriptDirectory

revision = "terminal_route_bootstrap_1029"
down_revision = "terminal_lane_route_1024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Reuse the canonical idempotent install, batched backfill and index recovery.
    parent = ScriptDirectory.from_config(context.config).get_revision(down_revision)
    parent.module.upgrade()


def downgrade() -> None:
    # The parent revision owns these routes; rollback retains its contract.
    pass
