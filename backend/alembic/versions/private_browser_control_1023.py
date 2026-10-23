"""Retain private browser ownership, fencing and recovery status."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "private_browser_control_1023"
down_revision = "delegation_permissions_1022"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "environment_browsers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("environment_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id", ondelete="SET NULL")),
        sa.Column("principal_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("desired_state", sa.String(16), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("control_mode", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("control_epoch", sa.Integer(), nullable=False),
        sa.Column("observed_epoch", sa.Integer(), nullable=False),
        sa.Column("control_expires_at", sa.DateTime(timezone=True)),
        sa.Column("viewer_expires_at", sa.DateTime(timezone=True)),
        sa.Column("worker_instance", sa.String(64), nullable=False),
        sa.Column("lifecycle_nonce", sa.String(64)),
        sa.Column("lifecycle_secret", sa.Text()),
        sa.Column("lifecycle_receipt_path", sa.Text()),
        sa.Column("resource_error", sa.String(128)),
        sa.Column("last_effect", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "environment_id"],
            ["execution_environments.tenant_id", "execution_environments.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('starting','ready','lost','stopped','revoked')", name="ck_environment_browser_status"
        ),
        sa.CheckConstraint(
            "control_mode IN ('agent','requested','human','paused')", name="ck_environment_browser_control"
        ),
        sa.CheckConstraint(
            "desired_state IN ('running','stopped') AND generation > 0", name="ck_environment_browser_desired"
        ),
        sa.CheckConstraint(
            "revision > 0 AND control_epoch > 0 AND observed_epoch >= 0", name="ck_environment_browser_versions"
        ),
    )
    for column in ("tenant_id", "environment_id", "agent_id", "session_id", "principal_id"):
        op.create_index(f"ix_environment_browsers_{column}", "environment_browsers", [column])
    op.create_index(
        "uq_environment_browser_active",
        "environment_browsers",
        ["environment_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('starting','ready','lost')"),
    )
    op.execute("ALTER TABLE environment_browsers ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE environment_browsers FORCE ROW LEVEL SECURITY")
    predicate = "current_setting('app.current_tenant_id', true) = 'BYPASS' OR tenant_id::text = current_setting('app.current_tenant_id', true)"
    op.execute(f"CREATE POLICY tenant_isolation ON environment_browsers USING ({predicate}) WITH CHECK ({predicate})")


def downgrade():
    connection = op.get_bind()
    previous = connection.scalar(sa.text("SELECT current_setting('app.current_tenant_id', true)"))
    try:
        connection.execute(sa.text("SELECT set_config('app.current_tenant_id', 'BYPASS', true)"))
        if connection.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM environment_browsers)")):
            raise RuntimeError("Retain private browser resource and control evidence before downgrade")
    finally:
        connection.execute(
            sa.text("SELECT set_config('app.current_tenant_id', :scope, true)"), {"scope": previous or ""}
        )
    op.drop_table("environment_browsers")
