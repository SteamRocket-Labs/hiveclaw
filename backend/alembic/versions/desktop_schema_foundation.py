"""Desktop schema foundation: agents Main/Sub model, agent_templates Role Template, tenants sync_version.

ARCHITECTURE.md §6.1, §6.2, §6.6 — Phase 2 prerequisite for Bootstrap/Sync endpoints.

Revision ID: desktop_schema_foundation_0326
Revises: add_refresh_tokens_0326
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "desktop_schema_foundation_0326"
down_revision: Union[str, Sequence[str], None] = "add_refresh_tokens_0326"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    """Check if a column exists in the given table (PostgreSQL)."""
    from sqlalchemy import text
    conn = op.get_bind()
    result = conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return result.scalar() is not None


def _add_column_safe(table: str, column: sa.Column) -> None:
    """Add a column only if it does not already exist."""
    if not _column_exists(table, column.name):
        op.add_column(table, column)


def _index_exists(index_name: str) -> bool:
    from sqlalchemy import text
    conn = op.get_bind()
    result = conn.execute(
        text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
        {"name": index_name},
    )
    return result.scalar() is not None


def _constraint_exists(constraint_name: str) -> bool:
    from sqlalchemy import text
    conn = op.get_bind()
    result = conn.execute(
        text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
        {"name": constraint_name},
    )
    return result.scalar() is not None


def upgrade() -> None:
    # ── agents table: Main/Sub agent model (§6.1) ──
    _add_column_safe("agents", sa.Column("agent_kind", sa.String(10), nullable=False, server_default="main"))
    _add_column_safe("agents", sa.Column("parent_agent_id", UUID(as_uuid=True), nullable=True))
    _add_column_safe("agents", sa.Column("owner_user_id", UUID(as_uuid=True), nullable=True))
    _add_column_safe("agents", sa.Column("channel_perms", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    _add_column_safe("agents", sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"))

    if not _constraint_exists("fk_agents_parent_agent_id"):
        op.create_foreign_key("fk_agents_parent_agent_id", "agents", "agents", ["parent_agent_id"], ["id"])
    if not _constraint_exists("fk_agents_owner_user_id"):
        op.create_foreign_key("fk_agents_owner_user_id", "agents", "users", ["owner_user_id"], ["id"])
    if not _index_exists("ix_agents_owner_user_id"):
        op.create_index("ix_agents_owner_user_id", "agents", ["owner_user_id"])
    if not _index_exists("ix_agents_parent_agent_id"):
        op.create_index("ix_agents_parent_agent_id", "agents", ["parent_agent_id"])

    # Partial unique index: each owner can have at most one main agent
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_owner_main ON agents (owner_user_id) "
        "WHERE agent_kind = 'main' AND owner_user_id IS NOT NULL"
    )
    # Check constraint: only main agents can have channel_perms=true
    if not _constraint_exists("ck_agents_channel_perms_main"):
        op.execute(
            "ALTER TABLE agents ADD CONSTRAINT ck_agents_channel_perms_main "
            "CHECK (channel_perms = false OR agent_kind = 'main')"
        )

    # ── agent_templates table: Role Template extensions (§6.2) ──
    _add_column_safe("agent_templates", sa.Column("tenant_id", UUID(as_uuid=True), nullable=True))
    _add_column_safe("agent_templates", sa.Column("department_id", UUID(as_uuid=True), nullable=True))
    _add_column_safe("agent_templates", sa.Column("model_id", UUID(as_uuid=True), nullable=True))
    _add_column_safe("agent_templates", sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"))

    if not _constraint_exists("fk_agent_templates_tenant_id"):
        op.create_foreign_key("fk_agent_templates_tenant_id", "agent_templates", "tenants", ["tenant_id"], ["id"])
    if not _constraint_exists("fk_agent_templates_department_id"):
        op.create_foreign_key("fk_agent_templates_department_id", "agent_templates", "departments", ["department_id"], ["id"])
    if not _constraint_exists("fk_agent_templates_model_id"):
        op.create_foreign_key("fk_agent_templates_model_id", "agent_templates", "llm_models", ["model_id"], ["id"])
    if not _index_exists("ix_agent_templates_tenant_id"):
        op.create_index("ix_agent_templates_tenant_id", "agent_templates", ["tenant_id"])

    # ── tenants table: sync_version (§6.6) ──
    _add_column_safe("tenants", sa.Column("sync_version", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    # tenants
    op.drop_column("tenants", "sync_version")

    # agent_templates
    op.drop_index("ix_agent_templates_tenant_id", table_name="agent_templates")
    op.drop_constraint("fk_agent_templates_model_id", "agent_templates", type_="foreignkey")
    op.drop_constraint("fk_agent_templates_department_id", "agent_templates", type_="foreignkey")
    op.drop_constraint("fk_agent_templates_tenant_id", "agent_templates", type_="foreignkey")
    op.drop_column("agent_templates", "config_version")
    op.drop_column("agent_templates", "model_id")
    op.drop_column("agent_templates", "department_id")
    op.drop_column("agent_templates", "tenant_id")

    # agents
    op.execute("ALTER TABLE agents DROP CONSTRAINT IF EXISTS ck_agents_channel_perms_main")
    op.execute("DROP INDEX IF EXISTS uq_agents_owner_main")
    op.drop_index("ix_agents_parent_agent_id", table_name="agents")
    op.drop_index("ix_agents_owner_user_id", table_name="agents")
    op.drop_constraint("fk_agents_owner_user_id", "agents", type_="foreignkey")
    op.drop_constraint("fk_agents_parent_agent_id", "agents", type_="foreignkey")
    op.drop_column("agents", "config_version")
    op.drop_column("agents", "channel_perms")
    op.drop_column("agents", "owner_user_id")
    op.drop_column("agents", "parent_agent_id")
    op.drop_column("agents", "agent_kind")
