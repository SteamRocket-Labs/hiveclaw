"""Bind business tool permissions to their exact native execution child."""

from alembic import op
import sqlalchemy as sa

from migration_snapshots.business_permissions_1022 import build_session_tenant_binding_function_sql

revision = 'business_permissions_1022'
down_revision = 'business_task_child_1022'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(build_session_tenant_binding_function_sql())


def downgrade():
    from migration_snapshots.business_task_child_1022 import (
        build_session_tenant_binding_function_sql as previous_sql,
    )

    connection = op.get_bind()
    previous = connection.scalar(sa.text("SELECT current_setting('app.current_tenant_id', true)"))
    try:
        connection.execute(sa.text("SELECT set_config('app.current_tenant_id', 'BYPASS', true)"))
        populated = connection.scalar(sa.text("""SELECT EXISTS(
            SELECT 1 FROM session_control_inputs AS control JOIN runtime_tasks AS run ON run.id=control.expected_run_id
            WHERE run.task_type='business_task' AND control.kind='permission_response'
              AND replace(lower(run.child_session_id),'-','')=replace(control.session_id::text,'-','')
        )"""))
        if populated:
            raise RuntimeError('Retain native business permission responses before downgrade')
    finally:
        connection.execute(sa.text("SELECT set_config('app.current_tenant_id', :scope, true)"), {'scope': previous or ''})
    op.execute(previous_sql())
