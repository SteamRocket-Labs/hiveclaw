"""Add guard_policies table for Desktop policy sync.

ARCHITECTURE.md §6.3 — one policy per tenant, Cloud-managed, Desktop-enforced.

Revision ID: add_guard_policies_0326
Revises: desktop_schema_foundation_0326
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


revision: str = "add_guard_policies_0326"
down_revision: Union[str, Sequence[str], None] = "desktop_schema_foundation_0326"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    table_exists = conn.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'guard_policies')")
    ).scalar()
    if table_exists:
        return

    op.create_table(
        "guard_policies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False, unique=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("zone_guard", JSONB(), nullable=False, server_default="{}"),
        sa.Column("egress_guard", JSONB(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_guard_policies_tenant_id", "guard_policies", ["tenant_id"], unique=True)


def downgrade() -> None:
    conn = op.get_bind()
    table_exists = conn.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'guard_policies')")
    ).scalar()
    if not table_exists:
        return

    op.drop_index("ix_guard_policies_tenant_id", table_name="guard_policies")
    op.drop_table("guard_policies")
