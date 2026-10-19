"""Persist governed remote A2A endpoints and encrypted machine credentials."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "remote_a2a_connections_1020"
down_revision = "machine_task_authority_1019"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "remote_a2a_connections",
        sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id"), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("owner_epoch", sa.Integer(), nullable=False),
        sa.Column("configured_by_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("card_url", sa.String(2048), nullable=False),
        sa.Column("rpc_url", sa.String(2048), nullable=False),
        sa.Column("credential_config", sa.JSON(), nullable=False),
        sa.Column("credential_configured", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("allowed_networks", JSONB, nullable=False),
        sa.Column("max_response_bytes", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("request_id", UUID(as_uuid=True), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("agent_card", JSONB),
        sa.Column("discovery_error", sa.String(100)),
        sa.Column("discovered_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("state IN ('configured','discovered','disabled')", name="ck_remote_a2a_connection_state"),
    )
    op.create_index("ix_remote_a2a_connections_tenant_id", "remote_a2a_connections", ["tenant_id"])
    op.execute("ALTER TABLE remote_a2a_connections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE remote_a2a_connections FORCE ROW LEVEL SECURITY")
    predicate = "current_setting('app.current_tenant_id', true) = 'BYPASS' OR tenant_id::text = current_setting('app.current_tenant_id', true)"
    op.execute(f"CREATE POLICY tenant_isolation ON remote_a2a_connections USING ({predicate}) WITH CHECK ({predicate})")


def downgrade():
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    if op.get_bind().scalar(sa.text("SELECT 1 FROM remote_a2a_connections LIMIT 1")):
        raise RuntimeError("Retain remote endpoint and credential ownership evidence before downgrading")
    op.drop_table("remote_a2a_connections")
