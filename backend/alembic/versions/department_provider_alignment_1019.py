"""Keep department mappings distinct by directory provider and identity namespace."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from app.migration_compat import add_column_if_missing, create_check_constraint_if_missing

revision = "department_provider_links_1019"
down_revision = "company_directory_namespace_1019"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing(
        op,
        "department_directory_links",
        sa.Column("provider_id", UUID(as_uuid=True), sa.ForeignKey("identity_providers.id", ondelete="RESTRICT")),
    )
    op.drop_constraint("ck_department_directory_links_provider", "department_directory_links", type_="check")
    create_check_constraint_if_missing(
        op,
        "ck_department_directory_links_provider",
        "department_directory_links",
        "provider IN ('feishu','dingtalk','wecom')",
    )
    create_check_constraint_if_missing(
        op,
        "ck_department_directory_links_namespace",
        "department_directory_links",
        "provider = 'feishu' OR provider_id IS NOT NULL",
    )
    op.drop_index("uq_department_directory_links_active_department", table_name="department_directory_links")
    op.create_index(
        "uq_department_directory_links_active_department",
        "department_directory_links",
        ["tenant_id", "provider", "department_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND department_id IS NOT NULL"),
    )


def downgrade():
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    if op.get_bind().scalar(
        sa.text(
            "SELECT 1 FROM department_directory_links WHERE provider <> 'feishu' OR provider_id IS NOT NULL LIMIT 1"
        )
    ):
        raise RuntimeError("Retain provider department mappings before downgrading")
    op.drop_index("uq_department_directory_links_active_department", table_name="department_directory_links")
    op.create_index(
        "uq_department_directory_links_active_department",
        "department_directory_links",
        ["tenant_id", "department_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND department_id IS NOT NULL"),
    )
    op.drop_constraint("ck_department_directory_links_namespace", "department_directory_links", type_="check")
    op.drop_constraint("ck_department_directory_links_provider", "department_directory_links", type_="check")
    op.create_check_constraint(
        "ck_department_directory_links_provider", "department_directory_links", "provider = 'feishu'"
    )
    op.drop_column("department_directory_links", "provider_id")
