"""Persist project organization without copying workspace or Session content."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from app.migration_compat import create_table_if_missing, create_index_if_missing

revision = "workbench_projects_1019"
down_revision = "private_environment_1018"
branch_labels = None
depends_on = None


def upgrade():
    create_table_if_missing(
        op,
        "workbench_projects",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_workbench_project_tenant_id"),
        sa.CheckConstraint("revision > 0", name="ck_workbench_project_revision"),
    )
    create_table_if_missing(
        op,
        "workbench_project_entries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("target_kind", sa.String(24), nullable=False),
        sa.Column("target_id", UUID(as_uuid=True), nullable=False),
        sa.Column("section", sa.String(100), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"], ["workbench_projects.tenant_id", "workbench_projects.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("target_kind IN ('workspace_file','session')", name="ck_workbench_project_entry_kind"),
    )
    for table, columns in (
        ("workbench_projects", ("tenant_id", "owner_user_id")),
        ("workbench_project_entries", ("tenant_id", "project_id")),
    ):
        for column in columns:
            create_index_if_missing(op, f"ix_{table}_{column}", table, [column])
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        predicate = f"current_setting('app.current_tenant_id', true) = 'BYPASS' OR {table}.tenant_id::text = current_setting('app.current_tenant_id', true)"
        op.execute(f'CREATE POLICY tenant_isolation ON "{table}" USING ({predicate}) WITH CHECK ({predicate})')


def downgrade():
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    if op.get_bind().scalar(sa.text("SELECT 1 FROM workbench_projects LIMIT 1")):
        raise RuntimeError("Retain project organization and references before downgrading")
    op.drop_table("workbench_project_entries")
    op.drop_table("workbench_projects")
