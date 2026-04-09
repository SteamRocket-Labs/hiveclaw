"""Merge current Alembic heads before tenant alignment changes.

Revision ID: merge_tenant_heads_20260325
Revises: add_security_audit_and_rbac, df3da9cf3b27
"""

from typing import Sequence, Union


revision: str = "merge_tenant_heads_20260325"
down_revision: Union[str, Sequence[str], None] = ("add_security_audit_and_rbac", "df3da9cf3b27")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
