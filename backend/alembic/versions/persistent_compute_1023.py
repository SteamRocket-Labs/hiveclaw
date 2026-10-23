"""Retain the exact private compute resource through command leases."""

from alembic import op
import sqlalchemy as sa

from app.migration_compat import add_column_if_missing

revision = "persistent_compute_1023"
down_revision = "ingress_event_namespace_1023"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing(
        op, "execution_environments", sa.Column("resource_binding", sa.JSON(none_as_null=True), nullable=True)
    )


def downgrade():
    connection = op.get_bind()
    previous = connection.scalar(sa.text("SELECT current_setting('app.current_tenant_id', true)"))
    try:
        connection.execute(sa.text("SELECT set_config('app.current_tenant_id', 'BYPASS', true)"))
        if connection.scalar(
            sa.text("SELECT EXISTS(SELECT 1 FROM execution_environments WHERE resource_binding IS NOT NULL)")
        ):
            raise RuntimeError("Retain or reconcile compute resource bindings before downgrade")
    finally:
        connection.execute(
            sa.text("SELECT set_config('app.current_tenant_id', :scope, true)"), {"scope": previous or ""}
        )
    op.drop_column("execution_environments", "resource_binding")
