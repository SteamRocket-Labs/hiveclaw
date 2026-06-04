"""RLS semantics against real PostgreSQL — §9 P0 red tests.

What this file proves (all four require a real PG; monkeypatched sessions
cannot observe any of it):

1. the production policy shape isolates tenants for **non-owner** roles;
2. an empty GUC fails closed for non-owner roles (no tenant rows visible);
3. **ENABLE-only RLS does NOT apply to the table owner** — production
   connects as the single ``DATABASE_URL`` user that also ran the
   migrations, i.e. the table owner, so today every production connection
   silently bypasses the tenant policies. Documented gap; the
   ``FORCE ROW LEVEL SECURITY`` decision lands with P1 (workflow tables are
   born FORCEd, legacy tables get an audited switch).
4. the alembic chain actually applied RLS + policies to production tables.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from tests.integration.conftest import APP_USER

TENANT_A = str(uuid.uuid4())
TENANT_B = str(uuid.uuid4())

# Same policy shape as alembic/versions/add_row_level_security.py — keep in
# sync so the probe table proves exactly what production tables enforce.
_PROBE_POLICY_SQL = """
    CREATE POLICY tenant_isolation_rls_probe ON rls_probe
        USING (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
            OR tenant_id IS NULL
        )
"""

_RLS_PRODUCTION_TABLES = [
    "agents",
    "users",
    "llm_models",
    "skills",
    "tools",
    "plaza_posts",
    "org_departments",
    "org_members",
    "config_revisions",
]


async def _create_probe_table(owner_engine) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS rls_probe"))
        await conn.execute(text("CREATE TABLE rls_probe (id uuid PRIMARY KEY, tenant_id uuid, note text)"))
        await conn.execute(text("ALTER TABLE rls_probe ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text(_PROBE_POLICY_SQL))
        await conn.execute(text(f"GRANT ALL ON rls_probe TO {APP_USER}"))
        await conn.execute(
            text("INSERT INTO rls_probe (id, tenant_id, note) VALUES (:ia, :ta, 'a'), (:ib, :tb, 'b')"),
            {"ia": uuid.uuid4(), "ta": TENANT_A, "ib": uuid.uuid4(), "tb": TENANT_B},
        )


async def _drop_probe_table(owner_engine) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS rls_probe"))


async def test_non_owner_with_tenant_guc_sees_only_own_rows(owner_engine, app_user_engine):
    await _create_probe_table(owner_engine)
    try:
        async with app_user_engine.connect() as conn:
            await conn.execute(text(f"SET LOCAL app.current_tenant_id = '{TENANT_A}'"))
            rows = (await conn.execute(text("SELECT tenant_id::text FROM rls_probe"))).scalars().all()
        assert rows == [TENANT_A]
    finally:
        await _drop_probe_table(owner_engine)


async def test_non_owner_with_empty_guc_sees_no_tenant_rows(owner_engine, app_user_engine):
    """Fail-closed default: unset/empty GUC matches nothing with a tenant_id."""
    await _create_probe_table(owner_engine)
    try:
        async with app_user_engine.connect() as conn:
            rows = (await conn.execute(text("SELECT tenant_id::text FROM rls_probe"))).scalars().all()
        assert rows == []
    finally:
        await _drop_probe_table(owner_engine)


async def test_table_owner_bypasses_enable_only_rls_documented_gap(owner_engine):
    """Production's DATABASE_URL user IS the table owner → ENABLE-only RLS is
    inert on every production connection. This test pins the gap so P1's
    FORCE decision has a red/green anchor to flip."""
    await _create_probe_table(owner_engine)
    try:
        async with owner_engine.connect() as conn:
            await conn.execute(text("SET LOCAL app.current_tenant_id = ''"))
            count = (await conn.execute(text("SELECT count(*) FROM rls_probe"))).scalar()
        # Empty GUC should hide both tenant rows — but the owner sees them all.
        assert count == 2
    finally:
        await _drop_probe_table(owner_engine)


async def test_alembic_applied_rls_to_production_tables(owner_engine):
    """``alembic upgrade head`` on an EMPTY database takes the bootstrap path
    (db_bootstrap: create_all + stamp head, skipping every migration) — the
    exact path every fresh deployment takes. P0 found that path shipped with
    ZERO RLS policies (and without config_revisions, which env.py forgot to
    import); both are fixed, and this test pins the contract: every tenant
    table exists with RLS enabled and its tenant_isolation_* policy in place
    (and documents that none are FORCEd yet — P1 flips that deliberately)."""
    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:names)"),
                {"names": _RLS_PRODUCTION_TABLES},
            )
        ).all()
        policies = (
            await conn.execute(
                text("SELECT tablename, policyname FROM pg_policies WHERE tablename = ANY(:names)"),
                {"names": _RLS_PRODUCTION_TABLES},
            )
        ).all()

    found = {row.relname: row for row in rows}
    assert set(found) == set(_RLS_PRODUCTION_TABLES), "migration chain must create every tenant table"
    for table in _RLS_PRODUCTION_TABLES:
        assert found[table].relrowsecurity is True, f"{table}: RLS not enabled by migration"
        # Documented gap (P1 closes it): ENABLE-only, not FORCEd.
        assert found[table].relforcerowsecurity is False, (
            f"{table}: unexpectedly FORCEd — update this documented-gap test and the P1 plan"
        )
    policy_tables = {tablename for tablename, _ in policies}
    assert policy_tables == set(_RLS_PRODUCTION_TABLES)
    assert all(name.startswith("tenant_isolation_") for _, name in policies)
