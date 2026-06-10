"""Backfill the 5 columns that existed only via entrypoint.sh ALTER patches.

``users.oidc_sub`` / ``users.oidc_issuer`` and
``security_audit_events.execution_identity_type`` / ``_id`` / ``_label`` were
added to production Docker DBs by entrypoint.sh's idempotent ALTER patches but
had NO Alembic migration. A non-Docker deploy running ``alembic upgrade head``
therefore never got them and ORM SELECTs 500'd (the patch-vs-migration schema
split, audit P0-4). This backfills them on the migration path with
``IF NOT EXISTS`` so it is idempotent and safe alongside the entrypoint patch,
which stays as a Docker safety net — the schema now has one complete source.

Revision ID: backfill_patch_only_columns_0609
Revises: retire_task_supervision_0608
Create Date: 2026-06-09
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "backfill_patch_only_columns_0609"
down_revision: Union[str, None] = "retire_task_supervision_0608"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :t"), {"t": table_name}
    )
    return result.scalar() is not None


# (table, idempotent DDL). Mirrors entrypoint.sh exactly so the two provisioning
# paths converge on the same columns.
_PATCHES: tuple[tuple[str, str], ...] = (
    ("users", "ALTER TABLE users ADD COLUMN IF NOT EXISTS oidc_sub VARCHAR(255) UNIQUE"),
    ("users", "ALTER TABLE users ADD COLUMN IF NOT EXISTS oidc_issuer VARCHAR(500)"),
    (
        "security_audit_events",
        "ALTER TABLE security_audit_events ADD COLUMN IF NOT EXISTS execution_identity_type VARCHAR(20)",
    ),
    (
        "security_audit_events",
        "ALTER TABLE security_audit_events ADD COLUMN IF NOT EXISTS execution_identity_id UUID",
    ),
    (
        "security_audit_events",
        "ALTER TABLE security_audit_events ADD COLUMN IF NOT EXISTS execution_identity_label VARCHAR(200)",
    ),
)


def upgrade() -> None:
    for table_name, sql in _PATCHES:
        if _table_exists(table_name):
            op.execute(text(sql))


def downgrade() -> None:
    # These columns are also provisioned by entrypoint.sh + create_all, so a
    # downgrade must not strand the ORM. Leave them in place (no-op).
    pass
