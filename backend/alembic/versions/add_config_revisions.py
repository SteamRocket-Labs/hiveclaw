"""Add config_revisions table for versioning agent/skill/prompt configurations.

Revision ID: add_config_revisions
Revises: add_llm_max_output_tokens
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "add_config_revisions"
down_revision = "add_llm_max_output_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    table_exists = conn.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'config_revisions')")
    ).scalar()
    if table_exists:
        return

    op.create_table(
        "config_revisions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("entity_type", sa.String(50), nullable=False, index=True),
        sa.Column("entity_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content", JSONB, nullable=False),
        sa.Column("diff_from_prev", JSONB, nullable=True),
        sa.Column("change_source", sa.String(20), nullable=False, server_default="user"),
        sa.Column("changed_by_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("changed_by_agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("change_message", sa.Text, nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("entity_type", "entity_id", "version", name="uq_config_revision_version"),
    )
    op.create_index("ix_config_revisions_entity", "config_revisions", ["entity_type", "entity_id"])
    op.create_index("ix_config_revisions_active", "config_revisions", ["entity_type", "entity_id", "is_active"],
                     postgresql_where=sa.text("is_active = true"))


def downgrade() -> None:
    conn = op.get_bind()
    table_exists = conn.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'config_revisions')")
    ).scalar()
    if table_exists:
        op.drop_table("config_revisions")
