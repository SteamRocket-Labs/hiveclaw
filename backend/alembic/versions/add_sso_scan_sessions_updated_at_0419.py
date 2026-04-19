"""Add missing updated_at column to sso_scan_sessions.

Revision ID: add_sso_scan_sessions_updated_at_0419
Revises: add_users_must_change_password_0418
Create Date: 2026-04-19

Production databases may already have sso_scan_sessions from an older manual
or partial migration path, but without updated_at. The ORM returns that column
on insert, so the Feishu SSO init path fails until the column is backfilled.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "add_sso_scan_sessions_updated_at_0419"
down_revision: Union[str, None] = "add_users_must_change_password_0418"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE sso_scan_sessions "
        "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
    )


def downgrade() -> None:
    op.drop_column("sso_scan_sessions", "updated_at")
