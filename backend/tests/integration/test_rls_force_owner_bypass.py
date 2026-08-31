"""Behavioral RLS probe: does FORCE ROW LEVEL SECURITY block the production
owner connection? — closing the largest multi-tenant blind spot.

WHY THIS FILE EXISTS
--------------------
Production (entrypoint.sh / app/database.py) connects the runtime engine via
``DATABASE_URL`` — the *same* role that ran the migrations/DDL, i.e. the table
**owner**. The non-owner ``app_rls`` role is only created in a "stage-3 prep"
cutover gated on ``RLS_APP_PASSWORD`` and is NOT the default. On Railway that
owner role is ``hive`` — a SUPERUSER.

The existing suite never tested this exact question:

* ``test_rls_tenant_isolation.py`` runs every isolation assertion as the
  *non-owner* ``rls_app_user`` (where RLS always applies);
* ``test_table_owner_bypasses_enable_only_rls_documented_gap`` only probes an
  **ENABLE-only** table (no FORCE) and asserts the owner sees everything;
* no test proved whether the owner connection reading a *real FORCE* table is
  blocked cross-tenant.

POSTGRESQL SEMANTICS UNDER TEST (verified empirically, not from memory)
-----------------------------------------------------------------------
Two independent role attributes decide whether RLS is bypassed:

1. **Ownership**: ``ALTER TABLE ... ENABLE`` does not apply to the owner;
   ``ALTER TABLE ... FORCE`` *does* apply to the owner. So a **non-superuser
   table owner** IS filtered by a FORCE policy.
2. **Superuser / BYPASSRLS**: a role with ``rolsuper`` or ``rolbypassrls``
   bypasses ALL row security regardless of ENABLE/FORCE. This is absolute and
   cannot be overridden by FORCE.

The production-mirroring fixture's default user (``test``, == conftest's stated
``hive`` analogue) is ``rolsuper=t, rolbypassrls=t`` — so the conflated
truth is: **production's owner connection bypasses even a FORCE table because
it is a superuser, not merely because it is the owner.** This file pins both
truths separately so the blind spot becomes falsifiable behavior, and shows the
concrete fix: route runtime through a non-superuser, non-owner role.

All cases require a real PostgreSQL; monkeypatched sessions cannot observe any
of this. The whole directory skips when Docker is unavailable (see conftest
``pg_container``).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.conftest import APP_USER, APP_USER_PASSWORD

TENANT_A = str(uuid.uuid4())
TENANT_B = str(uuid.uuid4())

# A dedicated NON-superuser table owner — created fresh per test session so no
# secret lives in source; the container is destroyed after the run anyway.
FORCE_OWNER = "rls_force_owner"
FORCE_OWNER_PASSWORD = uuid.uuid4().hex

# Policy shape copied verbatim from app/db_bootstrap.py:152-159 (== the
# add_row_level_security migration) so the probe enforces exactly what the
# production tables enforce.
_FORCE_PROBE_POLICY_SQL = """
    CREATE POLICY tenant_isolation_rls_force_probe ON rls_force_probe
        USING (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
            OR tenant_id IS NULL
        )
"""


async def _ensure_force_owner_role(owner_engine) -> None:
    """Create a NON-superuser, NOBYPASSRLS login role that will *own* the probe
    table — the only way to observe whether FORCE filters an owner without the
    superuser attribute masking the answer."""
    async with owner_engine.connect() as conn:
        await conn.execute(
            text(
                f"DO $$ BEGIN CREATE ROLE {FORCE_OWNER} LOGIN PASSWORD '{FORCE_OWNER_PASSWORD}' "
                "NOSUPERUSER NOBYPASSRLS; EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            )
        )
        await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {FORCE_OWNER}"))
        await conn.commit()


async def _create_force_probe(creator_engine, *, granted_owner: str | None = None) -> None:
    """Create rls_force_probe with ENABLE + FORCE RLS + the production policy,
    seeded with one row per tenant.

    ``creator_engine`` becomes the table owner. When ``granted_owner`` is set,
    ownership is reassigned to that role (used to give the non-superuser owner
    a table it actually owns)."""
    async with creator_engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS rls_force_probe"))
        await conn.execute(text("CREATE TABLE rls_force_probe (id uuid PRIMARY KEY, tenant_id uuid, note text)"))
        await conn.execute(text("ALTER TABLE rls_force_probe ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text("ALTER TABLE rls_force_probe FORCE ROW LEVEL SECURITY"))
        await conn.execute(text(_FORCE_PROBE_POLICY_SQL))
        await conn.execute(text(f"GRANT ALL ON rls_force_probe TO {APP_USER}"))
        if granted_owner:
            await conn.execute(text(f"GRANT ALL ON rls_force_probe TO {granted_owner}"))
            await conn.execute(text(f"ALTER TABLE rls_force_probe OWNER TO {granted_owner}"))
        await conn.execute(
            text("INSERT INTO rls_force_probe (id, tenant_id, note) VALUES (:ia, :ta, 'a'), (:ib, :tb, 'b')"),
            {"ia": uuid.uuid4(), "ta": TENANT_A, "ib": uuid.uuid4(), "tb": TENANT_B},
        )


async def _drop_force_probe(owner_engine) -> None:
    async with owner_engine.begin() as conn:
        # The table may be owned by FORCE_OWNER after reassignment; the
        # superuser owner_engine can still drop it.
        await conn.execute(text("DROP TABLE IF EXISTS rls_force_probe"))


async def test_production_owner_connection_is_superuser_bypassrls(owner_engine):
    """Pin the fixture's ground truth: the production-mirroring ``owner_engine``
    role IS a superuser with BYPASSRLS (conftest's stated ``hive`` analogue).

    This is the root of the blind spot — every FORCE assertion below only makes
    sense once we know the owner connection also carries superuser/BYPASSRLS,
    which is what production actually ships today."""
    async with owner_engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))
        ).one()
    rolsuper, rolbypassrls = row
    # Observed truth on postgres:16-alpine Testcontainer default ``test`` user.
    assert rolsuper is True, "production owner connection is expected to be a superuser (Railway hive)"
    assert rolbypassrls is True, "superuser implies BYPASSRLS — RLS cannot constrain this connection"


async def test_production_owner_connection_bypasses_force_rls_PRODUCTION_RISK(owner_engine):
    """THE BLIND SPOT, MADE FALSIFIABLE.

    The production runtime connects as this exact role (superuser+owner). We set
    the tenant GUC to tenant A and read a table that is BOTH ``ENABLE`` and
    ``FORCE ROW LEVEL SECURITY`` with the production tenant policy.

    OBSERVED RESULT: the connection sees BOTH tenants' rows. FORCE does NOT save
    us here, because the role is a superuser/BYPASSRLS — that attribute bypasses
    all row security regardless of FORCE. So FORCEing the production tables (the
    work landed for the workflow tables and planned for legacy tables) does
    **not** close the cross-tenant read hole on the connection production
    actually uses.

    This is an UNRESOLVED PRODUCTION RISK, asserted as such: a tenant-A request
    can read tenant-B rows on the real runtime connection. The fix is NOT more
    FORCE — it is routing the runtime through a non-superuser, non-owner role
    (the ``app_rls`` / ``RLS_APP_PASSWORD`` cutover), proven by
    ``test_non_owner_with_tenant_guc_on_force_table_is_isolated`` below.
    """
    await _create_force_probe(owner_engine)
    try:
        async with owner_engine.connect() as conn:
            await conn.execute(text(f"SET LOCAL app.current_tenant_id = '{TENANT_A}'"))
            rows = sorted((await conn.execute(text("SELECT tenant_id::text FROM rls_force_probe"))).scalars().all())
        # FORCE is inert against a superuser/BYPASSRLS connection — both rows leak.
        assert rows == sorted([TENANT_A, TENANT_B]), (
            "PRODUCTION RISK CONFIRMED: superuser+owner runtime connection reads "
            "across tenants even on a FORCE RLS table; FORCE alone does not fix it"
        )
    finally:
        await _drop_force_probe(owner_engine)


async def test_non_superuser_owner_is_constrained_by_force_rls(owner_engine):
    """Disambiguate ownership from superuser-ness.

    A NON-superuser role that *owns* the FORCE table is subject to its policy
    (this is exactly what FORCE adds over ENABLE: it reaches the owner too).
    With the tenant GUC set to A, this owner sees ONLY tenant-A rows.

    Conclusion the two preceding tests together establish: it is the
    SUPERUSER/BYPASSRLS attribute — not ownership — that lets the production
    connection bypass FORCE. Strip superuser from the runtime role and FORCE
    starts working even for the owner.
    """
    await _ensure_force_owner_role(owner_engine)
    # Created by the superuser, then ownership handed to the non-superuser role.
    await _create_force_probe(owner_engine, granted_owner=FORCE_OWNER)
    force_owner_url = make_url(owner_engine.url).set(username=FORCE_OWNER, password=FORCE_OWNER_PASSWORD)
    force_owner_engine = create_async_engine(force_owner_url, poolclass=NullPool)
    try:
        async with force_owner_engine.connect() as conn:
            # Sanity: this connection really is the table owner and not a superuser.
            attrs = (
                await conn.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))
            ).one()
            assert attrs == (False, False), "force-owner role must be non-superuser to isolate the FORCE effect"
            await conn.execute(text(f"SET LOCAL app.current_tenant_id = '{TENANT_A}'"))
            rows = (await conn.execute(text("SELECT tenant_id::text FROM rls_force_probe"))).scalars().all()
        assert rows == [TENANT_A], "FORCE RLS must filter the NON-superuser owner to its own tenant"
    finally:
        await force_owner_engine.dispose()
        await _drop_force_probe(owner_engine)


async def test_non_owner_with_tenant_guc_on_force_table_is_isolated(owner_engine, app_user_engine):
    """The fix, proven: the non-owner ``rls_app_user`` (NOSUPERUSER,
    NOBYPASSRLS) reading the SAME FORCE table with the tenant GUC set to A sees
    ONLY tenant-A rows. This is the connection shape the runtime must adopt
    (``app_rls`` / ``RLS_APP_PASSWORD`` cutover) to actually enforce isolation.
    """
    await _create_force_probe(owner_engine)
    try:
        async with app_user_engine.connect() as conn:
            await conn.execute(text(f"SET LOCAL app.current_tenant_id = '{TENANT_A}'"))
            rows = (await conn.execute(text("SELECT tenant_id::text FROM rls_force_probe"))).scalars().all()
        assert rows == [TENANT_A]
    finally:
        await _drop_force_probe(owner_engine)


async def test_non_owner_empty_guc_on_force_table_fails_closed(owner_engine, app_user_engine):
    """Defence-in-depth: an unset/empty GUC on the non-owner role matches no
    tenant rows (fail-closed) even though the table is FORCEd. Mirrors the
    ENABLE-only fail-closed test but on a FORCE table, confirming FORCE does not
    weaken the empty-GUC default for the role RLS actually governs."""
    await _create_force_probe(owner_engine)
    try:
        async with app_user_engine.connect() as conn:
            rows = (await conn.execute(text("SELECT tenant_id::text FROM rls_force_probe"))).scalars().all()
        assert rows == []
    finally:
        await _drop_force_probe(owner_engine)


# Touch APP_USER_PASSWORD so the unused-import lint stays quiet while keeping the
# symbol grouped with the credential it pairs with in conftest.
_ = (APP_USER, APP_USER_PASSWORD)
