"""Add tenants.memory_backend column.

Revision ID: add_tenant_memory_backend_0417
Revises: merge_channel_delivery_and_tenant_tool_heads_0413
Create Date: 2026-04-17

Enables per-tenant opt-in for Hindsight memory backend without touching
global MEMORY_BACKEND env var. Existing tenants default to NULL so the env
fallback still applies until an operator explicitly flips a tenant.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "add_tenant_memory_backend_0417"
down_revision: Union[str, None] = "merge_channel_delivery_and_tenant_tool_heads_0413"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CHECK_NAME = "ck_tenants_memory_backend_allowed"
_ALLOWED = ("md", "hindsight")


def upgrade() -> None:
    # NULL = "not set, use env fallback". Non-null literals ('md'/'hindsight')
    # are explicit per-tenant overrides. We cannot use server_default='md'
    # here because a persisted literal would shadow env MEMORY_BACKEND
    # for every tenant (resolution priority = non-null pref > env > default).
    #
    # IF NOT EXISTS guards against the entrypoint.sh safety-net patch that
    # creates the same column on startup — without it the alembic run fails
    # on every redeploy with DuplicateColumnError.
    op.execute(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS memory_backend VARCHAR(32)"
    )
    # DB-level validation so typos in future migrations or hand-edits don't
    # silently fall through to MD at resolve time.
    values_sql = ", ".join(f"'{v}'" for v in _ALLOWED)
    op.execute(
        f"ALTER TABLE tenants DROP CONSTRAINT IF EXISTS {_CHECK_NAME}"
    )
    op.create_check_constraint(
        _CHECK_NAME,
        "tenants",
        f"memory_backend IS NULL OR memory_backend IN ({values_sql})",
    )


def downgrade() -> None:
    op.drop_constraint(_CHECK_NAME, "tenants", type_="check")
    op.drop_column("tenants", "memory_backend")
