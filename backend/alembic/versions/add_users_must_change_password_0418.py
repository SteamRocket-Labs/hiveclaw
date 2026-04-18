"""Add users.must_change_password + reset Feishu-imported shadow passwords.

Revision ID: add_users_must_change_password_0418
Revises: add_tenant_memory_backend_0417
Create Date: 2026-04-18

Background
----------
Feishu org sync created shadow User rows with a random uuid as the password.
Nobody ever knew that password, so the imported employees could not actually
log in, and self-register hit 409 "email already exists" (their email was
already taken by the shadow row).

Fix
---
1. Add users.must_change_password flag (default False).
2. Reset every Feishu-imported shadow row (username LIKE 'feishu_%' AND
   feishu_open_id IS NOT NULL) to the shared default password "123456"
   and mark must_change_password=True so the UI nags them to rotate.

Safety
------
Only touches rows that look unambiguously like shadow imports (username
prefix + feishu_open_id). Real self-registered users are untouched.

The default-password hash is computed at migration time using bcrypt to
match hash_password() in app.core.security.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from passlib.context import CryptContext

revision: str = "add_users_must_change_password_0418"
down_revision: Union[str, None] = "add_tenant_memory_backend_0417"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULT_PW = "123456"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password "
        "BOOLEAN NOT NULL DEFAULT FALSE"
    )

    # Rehash once per upgrade; bcrypt is intentionally slow, no need for per-row.
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    default_hash = pwd_context.hash(_DEFAULT_PW)

    # Reset every Feishu shadow row. Escape the hash via bind param to avoid
    # any literal-quoting footguns (bcrypt hashes contain $ and /).
    op.execute(
        sa.text(
            "UPDATE users "
            "SET password_hash = :hash, must_change_password = TRUE "
            "WHERE username LIKE 'feishu_%' "
            "  AND feishu_open_id IS NOT NULL "
            "  AND must_change_password = FALSE"
        ).bindparams(hash=default_hash)
    )


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
