"""Index active terminal queues for bounded ordered identity scans."""

from alembic import op
import sqlalchemy as sa


revision = "terminal_queue_scan_1024"
down_revision = "terminal_lane_lookup_1024"
branch_labels = None
depends_on = None

_INDEX = "ix_runtime_terminal_boundary_ready_order"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        invalid = op.get_bind().scalar(sa.text(
            "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relname=:name "
            "AND (NOT i.indisvalid OR NOT i.indisready)"), {"name": _INDEX})
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
        op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
            "ON runtime_terminal_boundary_outbox (tenant_id, available_at, created_at, id) "
            "WHERE status IN ('pending','processing')")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
