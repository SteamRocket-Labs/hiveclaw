"""Keep enterprise bot installations separate from employee bot installations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "tenant_channel_principal_1018"
down_revision = "employee_readiness_probe_1018"
branch_labels = None
depends_on = None


def _binding_constraint(enterprise):
    method = "binding_method IN ('feishu_qr','feishu_tenant_identity')" if enterprise else "binding_method = 'feishu_qr'"
    op.drop_constraint("ck_external_principals_verified_binding", "external_principals", type_="check")
    op.create_check_constraint("ck_external_principals_verified_binding", "external_principals",
        "linked_user_id IS NULL OR (linked_at IS NOT NULL AND "
        "((provider = 'wechat_personal' AND binding_method = 'wechat_qr') OR "
        f"(provider = 'feishu' AND {method})) AND binding_verified_at IS NOT NULL)")


def upgrade():
    op.add_column("external_principals", sa.Column("tenant_channel_config_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_external_principals_tenant_channel", "external_principals", "tenant_channel_configs",
        ["tenant_channel_config_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_external_principals_tenant_channel_config_id", "external_principals", ["tenant_channel_config_id"])
    op.create_check_constraint("ck_external_principals_installation_kind", "external_principals",
        "channel_config_id IS NULL OR tenant_channel_config_id IS NULL")
    _binding_constraint(True)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT 1 FROM external_principals WHERE tenant_channel_config_id IS NOT NULL "
        "OR binding_method='feishu_tenant_identity' LIMIT 1")):
        raise RuntimeError("Retain enterprise channel identity evidence before downgrading; no bindings are deleted or relabeled")
    _binding_constraint(False)
    op.drop_constraint("ck_external_principals_installation_kind", "external_principals", type_="check")
    op.drop_index("ix_external_principals_tenant_channel_config_id", table_name="external_principals")
    op.drop_constraint("fk_external_principals_tenant_channel", "external_principals", type_="foreignkey")
    op.drop_column("external_principals", "tenant_channel_config_id")
