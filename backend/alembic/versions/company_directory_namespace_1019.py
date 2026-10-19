"""Namespace directory departments without rewriting legacy Feishu identities."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "company_directory_namespace_1019"
down_revision = "workbench_projects_1019"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("org_departments", sa.Column("provider_id", UUID(as_uuid=True), sa.ForeignKey("identity_providers.id")))
    op.add_column("org_departments", sa.Column("provider_department_id", sa.String(100)))
    op.create_unique_constraint("uq_org_department_provider_subject", "org_departments",
        ["tenant_id", "provider_id", "provider_department_id"])
    op.create_check_constraint("ck_org_department_provider_subject", "org_departments",
        "(provider_id IS NULL) = (provider_department_id IS NULL)")


def downgrade():
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    if op.get_bind().scalar(sa.text("SELECT 1 FROM org_departments WHERE provider_id IS NOT NULL LIMIT 1")):
        raise RuntimeError("Retain provider directory provenance before downgrading")
    op.drop_constraint("ck_org_department_provider_subject", "org_departments", type_="check")
    op.drop_constraint("uq_org_department_provider_subject", "org_departments", type_="unique")
    op.drop_column("org_departments", "provider_department_id")
    op.drop_column("org_departments", "provider_id")
