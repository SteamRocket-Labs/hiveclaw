"""Persist explicit remote employee runtime authorization without implicit activation."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "remote_a2a_runtime_1020"
down_revision = "remote_a2a_tasks_1020"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("remote_a2a_connections", sa.Column("runtime_authorization", postgresql.JSONB(), nullable=True))


def downgrade():
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    if op.get_bind().scalar(sa.text("SELECT 1 FROM agents WHERE agent_type='remote_a2a' LIMIT 1")) or op.get_bind().scalar(
        sa.text("SELECT 1 FROM runtime_tasks WHERE "
            "metadata_json->'remote_a2a'->>'dispatch_kind' IN ('employee', 'session') LIMIT 1")
    ):
        raise RuntimeError("The prior reader cannot validate remote employee dispatch history; keep this schema while that history exists")
    op.drop_column("remote_a2a_connections", "runtime_authorization")
