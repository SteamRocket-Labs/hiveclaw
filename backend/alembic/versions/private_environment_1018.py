"""Bind local command resources to durable private environments and leases."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "private_environment_1018"
down_revision = "tenant_channel_principal_1018"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "execution_environments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider_key", sa.String(64), nullable=False),
        sa.Column("desired_state", sa.String(16), nullable=False),
        sa.Column("observed_state", sa.String(16), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("fence_counter", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "agent_id", name="uq_execution_environment_private_agent"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_execution_environment_tenant_id"),
        sa.CheckConstraint("desired_state IN ('ready','stopped')", name="ck_execution_environment_desired"),
        sa.CheckConstraint(
            "observed_state IN ('ready','stopping','stopped','lost','failed')", name="ck_execution_environment_observed"
        ),
        sa.CheckConstraint(
            "generation > 0 AND revision > 0 AND fence_counter >= 0", name="ck_execution_environment_versions"
        ),
    )
    op.create_table(
        "environment_leases",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("environment_id", UUID(as_uuid=True), nullable=False),
        sa.Column("runtime_task_id", UUID(as_uuid=True), sa.ForeignKey("runtime_tasks.id", ondelete="SET NULL")),
        sa.Column("principal_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("invocation_key", sa.String(255), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("fence_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "environment_id"],
            ["execution_environments.tenant_id", "execution_environments.id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("status IN ('active','released')", name="ck_environment_lease_status"),
        sa.CheckConstraint("generation > 0 AND fence_version > 0", name="ck_environment_lease_versions"),
    )
    op.create_index(
        "uq_environment_active_writer",
        "environment_leases",
        ["environment_id"],
        unique=True,
        postgresql_where=sa.text("status='active'"),
    )
    for table, columns in (
        ("execution_environments", ("tenant_id", "agent_id")),
        ("environment_leases", ("tenant_id", "environment_id")),
    ):
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        predicate = f"current_setting('app.current_tenant_id', true) = 'BYPASS' OR {table}.tenant_id::text = current_setting('app.current_tenant_id', true)"
        op.execute(f'CREATE POLICY tenant_isolation ON "{table}" USING ({predicate}) WITH CHECK ({predicate})')


def downgrade():
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    if op.get_bind().scalar(sa.text("SELECT 1 FROM execution_environments LIMIT 1")):
        raise RuntimeError("Retain environment and command-lease evidence before downgrading")
    op.drop_table("environment_leases")
    op.drop_table("execution_environments")
