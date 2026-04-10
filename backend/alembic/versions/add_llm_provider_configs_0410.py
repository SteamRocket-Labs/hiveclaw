"""Add provider-first LLM configs and model sync fields.

Revision ID: add_llm_provider_configs_0410
Revises: add_agent_smart_model_routing_0410
Create Date: 2026-04-10
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "add_llm_provider_configs_0410"
down_revision: Union[str, None] = "add_agent_smart_model_routing_0410"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_provider_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("auth_mode", sa.String(length=32), nullable=False, server_default="api_key"),
        sa.Column("api_key_encrypted", sa.String(length=500), nullable=True),
        sa.Column("access_token_encrypted", sa.String(length=4000), nullable=True),
        sa.Column("refresh_token_encrypted", sa.String(length=4000), nullable=True),
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column("oauth_subject", sa.String(length=255), nullable=True),
        sa.Column("oauth_email", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "provider", name="uq_llm_provider_configs_tenant_provider"),
    )
    op.create_index("ix_llm_provider_configs_tenant_id", "llm_provider_configs", ["tenant_id"], unique=False)

    op.add_column("llm_models", sa.Column("temperature", sa.Float(), nullable=True))
    op.add_column("llm_models", sa.Column("provider_config_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        "llm_models",
        sa.Column("discovered_from_provider", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_llm_models_provider_config_id", "llm_models", ["provider_config_id"], unique=False)
    op.create_foreign_key(
        "fk_llm_models_provider_config_id",
        "llm_models",
        "llm_provider_configs",
        ["provider_config_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_llm_models_provider_config_id", "llm_models", type_="foreignkey")
    op.drop_index("ix_llm_models_provider_config_id", table_name="llm_models")
    op.drop_column("llm_models", "discovered_from_provider")
    op.drop_column("llm_models", "provider_config_id")
    op.drop_column("llm_models", "temperature")

    op.drop_index("ix_llm_provider_configs_tenant_id", table_name="llm_provider_configs")
    op.drop_table("llm_provider_configs")
