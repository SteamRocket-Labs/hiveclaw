"""Add tenant-scoped machine credentials without creating platform users."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = 'machine_credentials_1019'
down_revision = 'company_channel_binding_1019'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('machine_credentials',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', UUID(as_uuid=True), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('agent_id', UUID(as_uuid=True), sa.ForeignKey('agents.id'), nullable=False),
        sa.Column('principal_id', UUID(as_uuid=True), sa.ForeignKey('external_principals.id'), nullable=False),
        sa.Column('issued_by_user_id', UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('owner_user_id', UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('owner_epoch', sa.Integer(), nullable=False),
        sa.Column('issue_request_id', UUID(as_uuid=True), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('scopes', JSONB, nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True)),
        sa.Column('last_used_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('active','revoked')", name='ck_machine_credentials_status'),
        sa.UniqueConstraint('tenant_id', 'agent_id', 'issued_by_user_id', 'issue_request_id', name='uq_machine_credential_issue_request'))
    for column in ('tenant_id', 'agent_id', 'principal_id'):
        op.create_index(f'ix_machine_credentials_{column}', 'machine_credentials', [column])
    op.execute('ALTER TABLE machine_credentials ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE machine_credentials FORCE ROW LEVEL SECURITY')
    predicate = "current_setting('app.current_tenant_id', true) = 'BYPASS' OR tenant_id::text = current_setting('app.current_tenant_id', true)"
    op.execute(f'CREATE POLICY tenant_isolation ON machine_credentials USING ({predicate}) WITH CHECK ({predicate})')


def downgrade():
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    if op.get_bind().scalar(sa.text('SELECT 1 FROM machine_credentials LIMIT 1')):
        raise RuntimeError('Retain machine credential and principal evidence before downgrading')
    op.drop_table('machine_credentials')
