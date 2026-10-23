"""Persist self-service channel challenges without converting directory links to logins."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from app.migration_compat import create_table_if_missing, create_index_if_missing

revision = "company_channel_binding_1019"
down_revision = "department_provider_links_1019"
branch_labels = None
depends_on = None

_OLD = "linked_user_id IS NULL OR (linked_at IS NOT NULL AND ((provider = 'wechat_personal' AND binding_method = 'wechat_qr') OR (provider = 'feishu' AND binding_method IN ('feishu_qr','feishu_tenant_identity'))) AND binding_verified_at IS NOT NULL)"
_NEW = "linked_user_id IS NULL OR (linked_at IS NOT NULL AND ((provider = 'wechat_personal' AND binding_method = 'wechat_qr') OR (provider = 'feishu' AND binding_method IN ('feishu_qr','feishu_tenant_identity')) OR (provider IN ('dingtalk','wecom') AND tenant_channel_config_id IS NOT NULL AND binding_method = 'company_channel_challenge')) AND binding_verified_at IS NOT NULL)"


def upgrade():
    create_index_if_missing(
        op,
        "uq_company_dingtalk_messaging_app",
        "tenant_channel_configs",
        ["app_id"],
        unique=True,
        postgresql_where=sa.text(
            "channel_type = 'dingtalk' AND is_active AND extra_config -> 'messaging_enabled' = 'true'::jsonb"
        ),
    )
    create_table_if_missing(
        op,
        "company_channel_binding_challenges",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("config_id", UUID(as_uuid=True), sa.ForeignKey("tenant_channel_configs.id", ondelete="SET NULL")),
        sa.Column("identity_link_id", UUID(as_uuid=True), sa.ForeignKey("org_member_user_links.id"), nullable=False),
        sa.Column("link_version", sa.String(32), nullable=False),
        sa.Column("binding_history_version", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("installation_ref", sa.String(200), nullable=False),
        sa.Column("subject_id", sa.String(100), nullable=False),
        sa.Column("token_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("principal_id", UUID(as_uuid=True), sa.ForeignKey("external_principals.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("provider IN ('dingtalk','wecom')", name="ck_company_channel_challenge_provider"),
        sa.CheckConstraint(
            "(consumed_at IS NULL) = (principal_id IS NULL)", name="ck_company_channel_challenge_receipt"
        ),
    )
    for field in ("tenant_id", "user_id"):
        create_index_if_missing(
            op, f"ix_company_channel_binding_challenges_{field}", "company_channel_binding_challenges", [field]
        )
    op.execute("ALTER TABLE company_channel_binding_challenges ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE company_channel_binding_challenges FORCE ROW LEVEL SECURITY")
    predicate = "current_setting('app.current_tenant_id', true) = 'BYPASS' OR tenant_id::text = current_setting('app.current_tenant_id', true)"
    op.execute(
        f"CREATE POLICY tenant_isolation ON company_channel_binding_challenges USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.drop_constraint("ck_external_principals_verified_binding", "external_principals", type_="check")
    op.create_check_constraint("ck_external_principals_verified_binding", "external_principals", _NEW)


def downgrade():
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    if op.get_bind().scalar(sa.text("SELECT 1 FROM company_channel_binding_challenges LIMIT 1")):
        raise RuntimeError("Retain company binding proofs before downgrading")
    if op.get_bind().scalar(
        sa.text("SELECT 1 FROM external_principals WHERE binding_method='company_channel_challenge' LIMIT 1")
    ):
        raise RuntimeError("Retain verified company principals before downgrading")
    op.drop_constraint("ck_external_principals_verified_binding", "external_principals", type_="check")
    op.create_check_constraint("ck_external_principals_verified_binding", "external_principals", _OLD)
    op.drop_table("company_channel_binding_challenges")
    op.drop_index("uq_company_dingtalk_messaging_app", table_name="tenant_channel_configs")
