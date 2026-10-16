"""Add the per-user My Assistant binding.

Revision ID: m1f_assistant_bindings_1016
Revises: m1d_directory_observation_1016

The binding points at a native employee. It does not grant access.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "m1f_assistant_bindings_1016"
down_revision = "m1d_directory_observation_1016"
branch_labels = None
depends_on = None

ASSISTANT_TABLES = ("user_assistant_bindings",)

_STRICT_PREDICATE = """
    current_setting('app.current_tenant_id', true) = 'BYPASS'
    OR {table}.tenant_id::text = current_setting('app.current_tenant_id', true)
"""


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def _enable_strict_rls(table: str) -> None:
    predicate = _STRICT_PREDICATE.format(table=table)
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{table} ON {table}
        USING ({predicate})
        WITH CHECK ({predicate})
        """
    )


def upgrade() -> None:
    op.create_table(
        "user_assistant_bindings",
        sa.Column("id", _uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", _uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", _uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("agent_id", _uuid(), sa.ForeignKey("agents.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("prior_agent_id", _uuid(), sa.ForeignKey("agents.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id_at_bind", _uuid(), nullable=True),
        sa.Column("requester_user_id", _uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("provision_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active','provisioning','provision_incomplete')",
            name="ck_user_assistant_bindings_status",
        ),
        sa.CheckConstraint(
            "source IN ('explicit_selection','provisioned','legacy_provenance')",
            name="ck_user_assistant_bindings_source",
        ),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_user_assistant_bindings_tenant_user"),
    )
    op.create_index("ix_user_assistant_bindings_tenant_id", "user_assistant_bindings", ["tenant_id"])
    op.create_index("ix_user_assistant_bindings_agent_id", "user_assistant_bindings", ["agent_id"])
    _enable_strict_rls("user_assistant_bindings")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_user_assistant_bindings ON user_assistant_bindings")
    op.drop_index("ix_user_assistant_bindings_agent_id", table_name="user_assistant_bindings")
    op.drop_index("ix_user_assistant_bindings_tenant_id", table_name="user_assistant_bindings")
    op.drop_table("user_assistant_bindings")
