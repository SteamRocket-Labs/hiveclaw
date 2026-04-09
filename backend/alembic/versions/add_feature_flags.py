"""Add feature_flags table.

Revision ID: add_feature_flags
Revises: add_row_level_security
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY

revision = "add_feature_flags"
down_revision = "add_row_level_security"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    table_exists = conn.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'feature_flags')")
    ).scalar()
    if table_exists:
        return

    op.create_table(
        "feature_flags",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("key", sa.String(100), unique=True, nullable=False, index=True),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("flag_type", sa.String(20), nullable=False, server_default="boolean"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("rollout_percentage", sa.Integer, nullable=True),
        sa.Column("allowed_tenant_ids", ARRAY(UUID(as_uuid=True)), nullable=True),
        sa.Column("allowed_user_ids", ARRAY(UUID(as_uuid=True)), nullable=True),
        sa.Column("overrides", JSONB, nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    conn = op.get_bind()
    table_exists = conn.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'feature_flags')")
    ).scalar()
    if not table_exists:
        return

    op.drop_table("feature_flags")
