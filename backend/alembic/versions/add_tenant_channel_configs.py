"""Add tenant_channel_configs table for enterprise-level channel routing.

ARCHITECTURE.md Phase 6 — one bot per tenant, sender-based routing.

Revision ID: add_tenant_channel_configs_0326
Revises: add_guard_policies_0326
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


revision: str = "add_tenant_channel_configs_0326"
down_revision: Union[str, Sequence[str], None] = "add_guard_policies_0326"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    table_exists = conn.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'tenant_channel_configs')")
    ).scalar()
    if table_exists:
        return

    op.create_table(
        "tenant_channel_configs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("channel_type", sa.String(30), nullable=False),
        sa.Column("app_id", sa.String(255), nullable=True),
        sa.Column("app_secret", sa.String(255), nullable=True),
        sa.Column("encrypt_key", sa.String(255), nullable=True),
        sa.Column("verification_token", sa.String(255), nullable=True),
        sa.Column("extra_config", JSONB(), nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "channel_type", name="uq_tenant_channel_tenant_type"),
    )
    op.create_index("ix_tenant_channel_configs_tenant_id", "tenant_channel_configs", ["tenant_id"])


def downgrade() -> None:
    conn = op.get_bind()
    table_exists = conn.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'tenant_channel_configs')")
    ).scalar()
    if not table_exists:
        return

    op.drop_index("ix_tenant_channel_configs_tenant_id", table_name="tenant_channel_configs")
    op.drop_table("tenant_channel_configs")
