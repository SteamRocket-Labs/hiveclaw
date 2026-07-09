"""Repair external-capability RLS policies to strict tenant isolation.

The first FORCE migration used a second policy name on top of the create-table
policies. This hardening migration normalizes both possible policy names to a
single strict tenant policy, so already-upgraded databases do not retain an
older null-tenant bypass predicate.

Revision ID: external_capability_strict_rls_0709
Revises: runtime_budget_run_metadata_0709
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

revision = "external_capability_strict_rls_0709"
down_revision = "runtime_budget_run_metadata_0709"
branch_labels = None
depends_on = None


_EXTERNAL_CAPABILITY_TABLES: tuple[str, ...] = (
    "capability_factor_reviews",
    "capability_factors",
    "capability_promotion_proposals",
    "external_capability_reviews",
    "external_capability_snapshots",
    "external_extension_activations",
    "external_extension_catalog_entries",
    "external_extension_components",
    "external_extension_hook_registrations",
    "external_marketplace_entries",
    "external_marketplace_sources",
)


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _tenant_predicate(table: str) -> str:
    return f"""
        current_setting('app.current_tenant_id', true) = 'BYPASS'
        OR {table}.tenant_id::text = current_setting('app.current_tenant_id', true)
    """


def _install_strict_policy(table: str, *, policy_name: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
    op.execute(
        f"""
        CREATE POLICY {policy_name} ON {table}
        USING ({_tenant_predicate(table)})
        WITH CHECK ({_tenant_predicate(table)})
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    existing = _existing_tables()
    for table in _EXTERNAL_CAPABILITY_TABLES:
        if table not in existing:
            continue
        _install_strict_policy(table, policy_name=f"tenant_isolation_{table}")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    existing = _existing_tables()
    for table in _EXTERNAL_CAPABILITY_TABLES:
        if table not in existing:
            continue
        _install_strict_policy(table, policy_name=f"{table}_tenant_isolation")
