"""Remove agent_admin role — migrate to member, simplify to 3 roles.

Revision ID: remove_agent_admin_role_0327
Revises: add_tenant_channel_configs_0326
"""

from typing import Sequence, Union

from alembic import op


revision: str = "remove_agent_admin_role_0327"
down_revision: Union[str, Sequence[str], None] = "add_tenant_channel_configs_0326"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _enum_has_value(enum_name: str, value: str) -> bool:
    from sqlalchemy import text
    conn = op.get_bind()
    result = conn.execute(
        text("SELECT 1 FROM pg_enum WHERE enumtypid = CAST(:enum AS regtype) AND enumlabel = :val"),
        {"enum": enum_name, "val": value},
    )
    return result.scalar() is not None


def upgrade() -> None:
    # Only run if agent_admin still exists in the enum (idempotent)
    if not _enum_has_value("user_role_enum", "agent_admin"):
        return

    # Migrate existing agent_admin users to member
    op.execute("UPDATE users SET role = 'member' WHERE role = 'agent_admin'")

    # PostgreSQL enums can't drop values directly.
    # The safe approach: rename old enum, create new, migrate column, drop old.
    op.execute("ALTER TYPE user_role_enum RENAME TO user_role_enum_old")
    op.execute("CREATE TYPE user_role_enum AS ENUM ('platform_admin', 'org_admin', 'member')")
    op.execute(
        "ALTER TABLE users ALTER COLUMN role TYPE user_role_enum "
        "USING role::text::user_role_enum"
    )
    op.execute("DROP TYPE user_role_enum_old")


def downgrade() -> None:
    # Re-add agent_admin to enum
    op.execute("ALTER TYPE user_role_enum RENAME TO user_role_enum_old")
    op.execute("CREATE TYPE user_role_enum AS ENUM ('platform_admin', 'org_admin', 'agent_admin', 'member')")
    op.execute(
        "ALTER TABLE users ALTER COLUMN role TYPE user_role_enum "
        "USING role::text::user_role_enum"
    )
    op.execute("DROP TYPE user_role_enum_old")
