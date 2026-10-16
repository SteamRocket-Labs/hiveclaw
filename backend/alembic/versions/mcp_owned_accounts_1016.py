"""Separate owned MCP accounts, account-use grants and original-work choices."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "mcp_owned_accounts_1016"
down_revision = "mcp_oauth_encryption_1016"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("mcp_servers")}
    for name, target, deletion in (
        ("owner_user_id", "users.id", "CASCADE"),
        ("parent_server_id", "mcp_servers.id", "SET NULL"),
    ):
        if name not in columns:
            op.add_column(
                "mcp_servers",
                sa.Column(name, UUID(as_uuid=True), sa.ForeignKey(target, ondelete=deletion), nullable=True),
            )
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_mcp_servers_{name} ON mcp_servers ({name})")
    if not sa.inspect(bind).has_table("mcp_account_grants"):
        op.create_table(
            "mcp_account_grants",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column(
                "account_id", UUID(as_uuid=True), sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
            sa.Column(
                "requester_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column(
                "granted_by_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.UniqueConstraint(
                "tenant_id", "account_id", "agent_id", "requester_user_id", name="uq_mcp_account_grant"
            ),
        )
    if not sa.inspect(bind).has_table("mcp_work_account_bindings"):
        op.create_table(
            "mcp_work_account_bindings",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column(
                "session_id", UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
            sa.Column(
                "server_id", UUID(as_uuid=True), sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column(
                "requester_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column(
                "account_id", UUID(as_uuid=True), sa.ForeignKey("mcp_servers.id", ondelete="SET NULL"), nullable=True
            ),
            sa.Column(
                "grant_id",
                UUID(as_uuid=True),
                sa.ForeignKey("mcp_account_grants.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("grant_version", sa.Integer(), nullable=True),
            sa.Column("authorization_id", sa.String(64), nullable=True),
            sa.UniqueConstraint(
                "tenant_id",
                "session_id",
                "agent_id",
                "server_id",
                "requester_user_id",
                name="uq_mcp_work_account_binding",
            ),
        )
    for table, indexes in {
        "mcp_account_grants": ("tenant_id", "account_id", "agent_id"),
        "mcp_work_account_bindings": ("tenant_id", "session_id"),
    }.items():
        for column in indexes:
            op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""DO $$ BEGIN CREATE POLICY tenant_isolation_{table} ON {table}
            USING (current_setting('app.current_tenant_id', true) = 'BYPASS'
                OR tenant_id::text = current_setting('app.current_tenant_id', true));
            EXCEPTION WHEN duplicate_object THEN NULL; END $$""")


def downgrade():
    # An old reader would mistake personal credential rows for company connections.
    bind = op.get_bind()
    op.execute("LOCK TABLE mcp_servers IN ACCESS EXCLUSIVE MODE")
    previous_scope = bind.scalar(sa.text("SELECT current_setting('app.current_tenant_id', true)"))
    # The schema-owning migration must inspect every tenant, including under FORCE RLS.
    bind.execute(sa.text("SELECT set_config('app.current_tenant_id', 'BYPASS', true)"))
    try:
        if bind.execute(sa.text("SELECT 1 FROM mcp_servers WHERE owner_user_id IS NOT NULL LIMIT 1")).first():
            raise RuntimeError("Owned MCP accounts must be explicitly reconciled before using an older reader")
    finally:
        bind.execute(
            sa.text("SELECT set_config('app.current_tenant_id', :scope, true)"), {"scope": previous_scope or ""}
        )
    op.drop_table("mcp_work_account_bindings")
    op.drop_table("mcp_account_grants")
    op.drop_column("mcp_servers", "parent_server_id")
    op.drop_column("mcp_servers", "owner_user_id")
