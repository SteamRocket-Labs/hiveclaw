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

import bcrypt
import sqlalchemy as sa
from alembic import op

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

    # Use bcrypt directly instead of passlib — passlib 1.7.x tries to read
    # bcrypt.__about__.__version__ which bcrypt 4.x removed, and crashes with
    # "error reading bcrypt version" when its CryptContext is freshly built
    # inside a migration. The app runtime's pre-initialized CryptContext
    # works fine, but that's app code we must not import from a migration.
    default_hash = bcrypt.hashpw(_DEFAULT_PW.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

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
