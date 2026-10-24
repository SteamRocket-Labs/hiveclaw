"""Index canonical task identities for tenant-scoped terminal lane claims."""

from alembic import op
import sqlalchemy as sa


revision = "terminal_lane_lookup_1024"
down_revision = "persistent_compute_1023"
branch_labels = None
depends_on = None

_INDEX = "ix_runtime_tasks_tenant_type_id"


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
            "ON runtime_tasks (tenant_id, task_type, id)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
