"""Stage-2b shadow exhaustion — completeness proof before the role flip.

(1) Every one of the 18 stage-2b tables has RLS enabled + a ``tenant_isolation_*``
    policy. A table the migration/bootstrap missed would stay fully readable
    cross-tenant under the non-owner role — worse than a crash. This is the
    authoritative "no table left behind" check.
(2) ``write_audit_log`` (the bare-session shadow site for ``audit_logs``, a
    stage-2b table) now derives + stamps tenant_id from the agent, so its rows
    are tenant-scoped after the flip instead of NULL (globally visible).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from app.scripts.backfill_stage2b_tenant_id import BACKFILL_PLAN

_STAGE2B_TABLES = sorted({s.table for s in BACKFILL_PLAN})


async def test_all_18_stage2b_tables_have_rls_policy(owner_engine):
    """The migration + db_bootstrap covered every stage-2b table."""
    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT relname, relrowsecurity FROM pg_class WHERE relname = ANY(:names)"),
                {"names": _STAGE2B_TABLES},
            )
        ).all()
        policies = (
            await conn.execute(
                text("SELECT tablename, policyname FROM pg_policies WHERE tablename = ANY(:names)"),
                {"names": _STAGE2B_TABLES},
            )
        ).all()

    found = {r.relname: r for r in rows}
    assert set(found) == set(_STAGE2B_TABLES), f"missing tables: {set(_STAGE2B_TABLES) - set(found)}"
    for t in _STAGE2B_TABLES:
        assert found[t].relrowsecurity is True, f"{t}: RLS not enabled by migration"
    policy_tables = {tn for tn, _ in policies}
    assert policy_tables == set(_STAGE2B_TABLES), f"missing policy: {set(_STAGE2B_TABLES) - policy_tables}"
    assert all(name.startswith("tenant_isolation_") for _, name in policies)


async def test_write_audit_log_stamps_tenant_from_agent(owner_sessionmaker, monkeypatch):
    """The audit_logs shadow site derives tenant_id from the agent (not NULL)."""
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services import audit_logger, tenant_resolver

    suffix = uuid.uuid4().hex[:10]
    async with owner_sessionmaker() as db:
        t = Tenant(name="T", slug=f"t-{suffix}")
        db.add(t)
        await db.flush()
        u = User(
            username=f"u-{suffix}",
            email=f"{suffix}@example.test",
            password_hash="x",
            display_name="U",
            tenant_id=t.id,
        )
        db.add(u)
        await db.flush()
        a = Agent(name="A", creator_id=u.id, tenant_id=t.id)
        db.add(a)
        await db.flush()
        tid, aid = t.id, a.id
        await db.commit()

    # write_audit_log uses the global async_session in BOTH audit_logger (the
    # INSERT) and tenant_resolver (the bypass resolve) — point both at the container.
    monkeypatch.setattr(audit_logger, "async_session", owner_sessionmaker)
    monkeypatch.setattr(tenant_resolver, "async_session", owner_sessionmaker)

    await audit_logger.write_audit_log(action="trigger_fire", agent_id=aid)

    async with owner_sessionmaker() as db:
        got = (
            await db.execute(
                text("SELECT tenant_id::text FROM audit_logs WHERE agent_id = :aid"),
                {"aid": aid},
            )
        ).scalar_one()
    assert got == str(tid)


async def test_write_audit_log_system_event_stays_null(owner_sessionmaker, monkeypatch):
    """A system-level event (no agent) keeps tenant_id NULL (operator-only)."""
    from app.services import audit_logger

    monkeypatch.setattr(audit_logger, "async_session", owner_sessionmaker)
    action = f"system_event_{uuid.uuid4().hex[:10]}"

    await audit_logger.write_audit_log(action=action, agent_id=None)

    async with owner_sessionmaker() as db:
        got = (
            await db.execute(
                text("SELECT tenant_id FROM audit_logs WHERE action = :a"),
                {"a": action},
            )
        ).scalar_one()
    assert got is None
