"""Scope durable provider event identities to their authenticated application."""

from alembic import op
import sqlalchemy as sa

from app.migration_compat import add_column_if_missing

revision = "ingress_event_namespace_1023"
down_revision = "tool_preflight_audit_1023"
branch_labels = None
depends_on = None

_CONSTRAINT = "uq_channel_ingress_provider_event"
_TABLE = "channel_ingress_events"
_OLD_KEY = ["tenant_id", "provider", "installation_ref", "provider_event_id"]


def upgrade():
    add_column_if_missing(op, _TABLE, sa.Column("provider_namespace", sa.String(64), nullable=False, server_default=""))
    connection = op.get_bind()
    previous = connection.scalar(sa.text("SELECT current_setting('app.current_tenant_id', true)"))
    try:
        connection.execute(sa.text("SELECT set_config('app.current_tenant_id', 'BYPASS', true)"))
        connection.execute(
            sa.text("""UPDATE channel_ingress_events
            SET provider_namespace = metadata_json->>'feishu_message_scope'
            WHERE provider = 'feishu' AND (metadata_json->>'feishu_message_scope') ~ '^[0-9a-f]{64}$'""")
        )
    finally:
        connection.execute(
            sa.text("SELECT set_config('app.current_tenant_id', :scope, true)"), {"scope": previous or ""}
        )
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="unique")
    op.create_unique_constraint(_CONSTRAINT, _TABLE, [*_OLD_KEY[:3], "provider_namespace", _OLD_KEY[3]])


def downgrade():
    connection = op.get_bind()
    previous = connection.scalar(sa.text("SELECT current_setting('app.current_tenant_id', true)"))
    try:
        connection.execute(sa.text("SELECT set_config('app.current_tenant_id', 'BYPASS', true)"))
        if connection.scalar(
            sa.text("""SELECT EXISTS(SELECT 1 FROM channel_ingress_events
            GROUP BY tenant_id, provider, installation_ref, provider_event_id HAVING count(*) > 1)""")
        ):
            raise RuntimeError("Retain distinct application event identities before downgrade")
    finally:
        connection.execute(
            sa.text("SELECT set_config('app.current_tenant_id', :scope, true)"), {"scope": previous or ""}
        )
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="unique")
    op.create_unique_constraint(_CONSTRAINT, _TABLE, _OLD_KEY)
    op.drop_column(_TABLE, "provider_namespace")
