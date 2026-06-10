"""Stage-2a RLS coverage against real PostgreSQL — the 21 previously-unpoliced
tenant tables are now ENABLEd + policied, closing the silent cross-tenant leak.

These tables carried ``tenant_id`` but were absent from the bootstrap RLS list,
so under the stage-3 non-owner role flip they would have stayed fully readable
across tenants. This pins that every one now has RLS enabled and its
``tenant_isolation_*`` policy (via the bootstrap path that every fresh
deployment runs), plus an end-to-end fail-closed check on a representative table.
"""

from __future__ import annotations

import importlib as _importlib
import pkgutil as _pkgutil
import uuid

from sqlalchemy import text

# Register every model so ORM seeds below can resolve cross-table foreign keys
# (Tenant/Department reference tenants/users/departments). Same completeness the
# alembic env.py relies on for stage-2a.
import app.models as _models_pkg

for _m in _pkgutil.iter_modules(_models_pkg.__path__):
    _importlib.import_module(f"app.models.{_m.name}")

_STAGE2A_TABLES = [
    "agent_mcp_server_assignments",
    "agent_mcp_tool_overrides",
    "agent_plan_recommendations",
    "agent_plan_requests",
    "agent_templates",
    "agent_work_ledgers",
    "capability_policies",
    "charter_proposals",
    "departments",
    "enterprise_info",
    "guard_policies",
    "identity_providers",
    "invitation_codes",
    "mcp_server_tools",
    "mcp_servers",
    "resource_permissions",
    "security_audit_events",
    "sso_scan_sessions",
    "tenant_channel_configs",
    "tenant_settings",
    "tenant_tool_configs",
]


async def test_stage2a_tables_enabled_and_policied(owner_engine):
    """Every stage-2a table has RLS enabled + its tenant_isolation_* policy."""
    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT relname, relrowsecurity FROM pg_class WHERE relname = ANY(:names)"),
                {"names": _STAGE2A_TABLES},
            )
        ).all()
        policies = (
            await conn.execute(
                text(
                    "SELECT tablename FROM pg_policies "
                    "WHERE tablename = ANY(:names) AND policyname LIKE 'tenant_isolation_%'"
                ),
                {"names": _STAGE2A_TABLES},
            )
        ).all()

    found = {r.relname for r in rows}
    assert found == set(_STAGE2A_TABLES), f"missing tables: {set(_STAGE2A_TABLES) - found}"
    enabled = {r.relname for r in rows if r.relrowsecurity}
    assert enabled == set(_STAGE2A_TABLES), f"RLS not enabled: {set(_STAGE2A_TABLES) - enabled}"
    policied = {t for (t,) in policies}
    assert policied == set(_STAGE2A_TABLES), f"policy missing: {set(_STAGE2A_TABLES) - policied}"


async def test_stage2a_nonowner_empty_guc_fails_closed(owner_sessionmaker, app_user_engine):
    """Representative stage-2a table (departments): the non-owner role with an
    empty GUC sees no tenant rows; a matching GUC sees its own — proving the
    previously-open table is now isolated. Seeded via ORM as the owner (bypasses
    RLS for setup and fills every column default)."""
    from app.models.tenant import Tenant
    from app.models.user import Department

    async with owner_sessionmaker() as s:
        tenant = Tenant(name="probe-t", slug=f"probe-{uuid.uuid4().hex[:8]}", im_provider="web_only")
        s.add(tenant)
        await s.flush()
        dept = Department(tenant_id=tenant.id, name="probe-dept")
        s.add(dept)
        await s.commit()
        tenant_id, dept_id = tenant.id, dept.id
    try:
        async with app_user_engine.connect() as conn:
            await conn.execute(text("SET LOCAL app.current_tenant_id = ''"))
            empty = (
                await conn.execute(
                    text("SELECT count(*) FROM departments WHERE tenant_id = :tid"), {"tid": str(tenant_id)}
                )
            ).scalar()
            await conn.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_id}'"))
            scoped = (
                await conn.execute(
                    text("SELECT count(*) FROM departments WHERE tenant_id = :tid"), {"tid": str(tenant_id)}
                )
            ).scalar()
        assert empty == 0, "empty GUC must fail-closed (was leaking cross-tenant before stage-2a)"
        assert scoped == 1, "matching GUC must see the tenant's own row"
    finally:
        async with owner_sessionmaker() as s:
            await s.execute(text("DELETE FROM departments WHERE id = :id"), {"id": str(dept_id)})
            await s.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": str(tenant_id)})
            await s.commit()
