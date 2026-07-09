"""Force tenant RLS on the external-capability and capability-factor tables.

These eleven tables landed with the marketplace/capability-intake commits but
never joined an RLS FORCE migration — the bootstrap force-list covered them at
create_all time only. Multi-tenancy is a hard invariant: every tenant-scoped
table carries a FORCE ROW LEVEL SECURITY policy at the migration layer too.

Revision ID: external_capability_rls_0709
Revises: runtime_budget_breaker_dims_0709
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

revision = "external_capability_rls_0709"
down_revision = "runtime_budget_breaker_dims_0709"
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
        OR {table}.tenant_id IS NULL
    """


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    existing = _existing_tables()
    for table in _EXTERNAL_CAPABILITY_TABLES:
        if table not in existing:
            continue
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING ({_tenant_predicate(table)})
            WITH CHECK ({_tenant_predicate(table)})
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    existing = _existing_tables()
    for table in _EXTERNAL_CAPABILITY_TABLES:
        if table not in existing:
            continue
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
