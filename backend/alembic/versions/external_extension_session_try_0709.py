"""Add session-scoped external extension activations.

Revision ID: external_extension_session_try_0709
Revises: external_extension_components_0709
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "external_extension_session_try_0709"
down_revision = "external_extension_components_0709"
branch_labels = None
depends_on = None


def _foreign_key_exists(table_name: str, constraint_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(item.get("name") == constraint_name for item in inspector.get_foreign_keys(table_name))


def upgrade() -> None:
    op.drop_constraint(
        "uq_external_extension_activation",
        "external_extension_activations",
        type_="unique",
        if_exists=True,
    )
    op.add_column(
        "external_extension_activations",
        sa.Column("activation_scope", sa.String(length=30), server_default="agent", nullable=False),
        if_not_exists=True,
    )
    op.add_column(
        "external_extension_activations",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        if_not_exists=True,
    )
    op.add_column(
        "external_extension_activations",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        if_not_exists=True,
    )
    if not _foreign_key_exists(
        "external_extension_activations",
        "fk_external_extension_activations_session_id_chat_sessions",
    ):
        op.create_foreign_key(
            "fk_external_extension_activations_session_id_chat_sessions",
            "external_extension_activations",
            "chat_sessions",
            ["session_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        op.f("ix_external_extension_activations_activation_scope"),
        "external_extension_activations",
        ["activation_scope"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_activations_session_id"),
        "external_extension_activations",
        ["session_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_external_extension_activations_expires_at"),
        "external_extension_activations",
        ["expires_at"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_external_extension_activation_agent_unique_active",
        "external_extension_activations",
        ["tenant_id", "agent_id", "snapshot_id"],
        unique=True,
        postgresql_where=sa.text("activation_scope = 'agent' AND status = 'active'"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_external_extension_activation_session_unique_active",
        "external_extension_activations",
        ["tenant_id", "agent_id", "snapshot_id", "session_id"],
        unique=True,
        postgresql_where=sa.text("activation_scope = 'session' AND status = 'active'"),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_external_extension_activation_session_unique_active",
        table_name="external_extension_activations",
        if_exists=True,
    )
    op.drop_index(
        "ix_external_extension_activation_agent_unique_active",
        table_name="external_extension_activations",
        if_exists=True,
    )
    op.drop_index(
        op.f("ix_external_extension_activations_expires_at"),
        table_name="external_extension_activations",
        if_exists=True,
    )
    op.drop_index(
        op.f("ix_external_extension_activations_session_id"),
        table_name="external_extension_activations",
        if_exists=True,
    )
    op.drop_index(
        op.f("ix_external_extension_activations_activation_scope"),
        table_name="external_extension_activations",
        if_exists=True,
    )
    op.drop_constraint(
        "fk_external_extension_activations_session_id_chat_sessions",
        "external_extension_activations",
        type_="foreignkey",
        if_exists=True,
    )
    op.drop_column("external_extension_activations", "expires_at")
    op.drop_column("external_extension_activations", "session_id")
    op.drop_column("external_extension_activations", "activation_scope")
    op.create_unique_constraint(
        "uq_external_extension_activation",
        "external_extension_activations",
        ["tenant_id", "agent_id", "snapshot_id"],
    )
