"""Persist independent machine requester authority on the existing runtime task."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = 'machine_task_authority_1019'
down_revision = 'machine_credentials_1019'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('runtime_tasks', sa.Column('root_external_principal_id', UUID(as_uuid=True),
        sa.ForeignKey('external_principals.id', ondelete='RESTRICT'), nullable=True))
    op.add_column('runtime_tasks', sa.Column('machine_credential_id', UUID(as_uuid=True),
        sa.ForeignKey('machine_credentials.id', ondelete='RESTRICT'), nullable=True))
    for column in ('root_external_principal_id', 'machine_credential_id'):
        op.create_index(f'ix_runtime_tasks_{column}', 'runtime_tasks', [column])
    for table, constraint in (('runtime_notification_outbox', 'ck_runtime_notification_outbox_requester_root'),
        ('runtime_result_integration_pages', 'ck_runtime_result_pages_requester_root')):
        op.alter_column(table, 'parent_user_id', nullable=True)
        op.create_check_constraint(constraint, table, 'parent_user_id IS NOT NULL OR root_runtime_task_id IS NOT NULL')


def downgrade():
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    if op.get_bind().scalar(sa.text('SELECT 1 FROM runtime_tasks WHERE machine_credential_id IS NOT NULL OR root_external_principal_id IS NOT NULL LIMIT 1')):
        raise RuntimeError('Retain machine task authority evidence before downgrading')
    for table, constraint in (('runtime_notification_outbox', 'ck_runtime_notification_outbox_requester_root'),
        ('runtime_result_integration_pages', 'ck_runtime_result_pages_requester_root')):
        if op.get_bind().scalar(sa.text(f'SELECT 1 FROM {table} WHERE parent_user_id IS NULL LIMIT 1')):
            raise RuntimeError('Retain machine result authority evidence before downgrading')
        op.drop_constraint(constraint, table, type_='check')
        op.alter_column(table, 'parent_user_id', nullable=False)
    for column in ('machine_credential_id', 'root_external_principal_id'):
        op.drop_index(f'ix_runtime_tasks_{column}', 'runtime_tasks')
        op.drop_column('runtime_tasks', column)
