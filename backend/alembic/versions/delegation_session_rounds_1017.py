"""Bind delegated Session V2 input/model/tool records to the exact durable child."""

from alembic import op
from migration_snapshots.delegation_session_rounds_1017 import build_session_tenant_binding_function_sql

revision = "delegation_session_rounds_1017"
down_revision = "custom_api_owned_accounts_1017"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(build_session_tenant_binding_function_sql())
    op.execute("REVOKE EXECUTE ON FUNCTION public.enforce_session_v2_tenant_binding() FROM PUBLIC")


def downgrade():
    # Retain exact-child authority while older workers drain already-persisted evidence.
    upgrade()
