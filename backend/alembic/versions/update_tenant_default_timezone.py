"""Update default timezone from UTC to Asia/Shanghai for existing tenants.

Revision ID: update_tenant_tz_0331
Revises: 82dbade25f85
Create Date: 2026-03-31

Existing tenants were created with timezone='UTC' (old default).
New default is 'Asia/Shanghai'. This migration updates existing rows
that still have the old default.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "update_tenant_tz_0331"
down_revision: Union[str, None] = "82dbade25f85"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE tenants SET timezone = 'Asia/Shanghai' WHERE timezone = 'UTC'")


def downgrade() -> None:
    op.execute("UPDATE tenants SET timezone = 'UTC' WHERE timezone = 'Asia/Shanghai'")
