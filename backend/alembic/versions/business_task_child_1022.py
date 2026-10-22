"""Bind native business model and tool receipts to their exact execution session."""

from alembic import op
import sqlalchemy as sa

from migration_snapshots.business_task_child_1022 import build_session_tenant_binding_function_sql

revision = 'business_task_child_1022'
down_revision = 'original_blob_generations_1021'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(build_session_tenant_binding_function_sql())


def downgrade():
    from migration_snapshots.delegation_session_rounds_1017 import (
        build_session_tenant_binding_function_sql as previous_authority_sql,
    )

    connection = op.get_bind()
    previous = connection.scalar(sa.text("SELECT current_setting('app.current_tenant_id', true)"))
    try:
        connection.execute(sa.text("SELECT set_config('app.current_tenant_id', 'BYPASS', true)"))
        populated = connection.scalar(sa.text("""SELECT EXISTS(
            SELECT 1 FROM session_model_results AS result JOIN runtime_tasks AS run ON run.id=result.run_id
            WHERE run.task_type='business_task'
        )"""))
        if populated:
            raise RuntimeError('Retain native business model and tool receipts before downgrade')
    finally:
        connection.execute(sa.text("SELECT set_config('app.current_tenant_id', :scope, true)"), {'scope': previous or ''})
    op.execute(previous_authority_sql())
