"""Keep personal API credentials separate from employee settings."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "custom_api_owned_accounts_1017"
down_revision = "email_owned_accounts_1017"
branch_labels = None
depends_on = None


def _id(name, target, *, nullable=False, deletion=None):
    return sa.Column(name, UUID(as_uuid=True), sa.ForeignKey(target, ondelete=deletion), nullable=nullable)


def upgrade():
    bind = op.get_bind()
    tables = {
        "custom_api_accounts": [
            _id("owner_user_id", "users.id"), _id("tool_id", "tools.id"), sa.Column("name", sa.String(200), nullable=False),
            sa.Column("connector_revision", sa.String(64), nullable=True), sa.Column("config", sa.JSON(), nullable=False), sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        ],
        "custom_api_account_grants": [
            _id("account_id", "custom_api_accounts.id"), _id("agent_id", "agents.id"), _id("requester_user_id", "users.id"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.UniqueConstraint("tenant_id", "account_id", "agent_id", "requester_user_id", name="uq_custom_api_account_grant"),
        ],
        "custom_api_work_account_bindings": [
            _id("session_id", "chat_sessions.id", deletion="CASCADE"), _id("agent_id", "agents.id"),
            _id("requester_user_id", "users.id"), _id("tool_id", "tools.id"), _id("account_id", "custom_api_accounts.id", nullable=True),
            _id("grant_id", "custom_api_account_grants.id", nullable=True),
            sa.Column("account_version", sa.Integer(), nullable=True), sa.Column("grant_version", sa.Integer(), nullable=True),
            sa.Column("company_revision", sa.String(64), nullable=True),
            sa.UniqueConstraint("tenant_id", "session_id", "agent_id", "requester_user_id", "tool_id", name="uq_custom_api_work_account_binding"),
        ],
    }
    for name, fields in tables.items():
        if not sa.inspect(bind).has_table(name):
            op.create_table(name, sa.Column("id", UUID(as_uuid=True), primary_key=True),
                _id("tenant_id", "tenants.id"), *fields)
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{name}_tenant_id ON {name} (tenant_id)")
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
        op.execute(f"""DO $$ BEGIN CREATE POLICY tenant_isolation_{name} ON {name}
            USING (current_setting('app.current_tenant_id', true) = 'BYPASS'
                OR tenant_id::text = current_setting('app.current_tenant_id', true));
            EXCEPTION WHEN duplicate_object THEN NULL; END $$""")


def downgrade():
    bind = op.get_bind()
    op.execute("LOCK TABLE custom_api_accounts, custom_api_account_grants, custom_api_work_account_bindings IN ACCESS EXCLUSIVE MODE")
    scope = bind.scalar(sa.text("SELECT current_setting('app.current_tenant_id', true)"))
    bind.execute(sa.text("SELECT set_config('app.current_tenant_id', 'BYPASS', true)"))
    try:
        if any(bind.scalar(sa.text(f"SELECT EXISTS(SELECT 1 FROM {name})")) for name in
            ("custom_api_accounts", "custom_api_account_grants", "custom_api_work_account_bindings")):
            raise RuntimeError("API accounts and work choices must be reconciled before downgrade")
    finally:
        bind.execute(sa.text("SELECT set_config('app.current_tenant_id', :scope, true)"), {"scope": scope or ""})
    for name in ("custom_api_work_account_bindings", "custom_api_account_grants", "custom_api_accounts"):
        op.drop_table(name)
