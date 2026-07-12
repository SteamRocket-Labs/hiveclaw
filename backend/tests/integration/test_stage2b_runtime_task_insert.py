"""Stage-2b: migrated INSERT paths set tenant_id against real PostgreSQL.

The stage-2b RLS policy on ``runtime_tasks`` is ``USING``-only (no
``WITH CHECK``): an INSERT that forgets ``tenant_id`` writes a NULL row that is
globally visible after the stage-3 role flip — an isolation hole. This proves
the accessor that creates runtime-task rows
(:func:`create_runtime_task_record`) derives ``tenant_id`` from the parent
agent and writes it, and leaves a parent-less row NULL (orphan surfaced, not
invented) — the mirror of the backfill's behaviour, but on the live write path.

A mock session can observe none of this (no RLS, no real INSERT), so this lives
in the Testcontainers integration suite.
"""

from __future__ import annotations

import uuid

import pytest

from app.database import tenant_scoped_session as real_tenant_scoped_session
from app.models.agent import Agent
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.services import runtime_task_service
from app.services.tenant_resolver import resolve_tenant_for_agent as real_resolve_tenant_for_agent


async def _mk_tenant(db) -> uuid.UUID:
    t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
    db.add(t)
    await db.flush()
    return t.id


async def _mk_user(db, tenant_id) -> uuid.UUID:
    u = User(
        username=f"u-{uuid.uuid4().hex[:10]}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        password_hash="x",
        display_name="U",
        tenant_id=tenant_id,
    )
    db.add(u)
    await db.flush()
    return u.id


async def _mk_agent(db, *, creator_id, tenant_id) -> uuid.UUID:
    a = Agent(name="A", creator_id=creator_id, tenant_id=tenant_id)
    db.add(a)
    await db.flush()
    return a.id


def _bind_accessors_to(monkeypatch, sessionmaker) -> None:
    """Point ``create_runtime_task_record``'s stage-2b accessors at the test
    engine: the real ``tenant_scoped_session`` / ``resolve_tenant_for_agent``,
    but threaded onto the Testcontainers sessionmaker instead of the app engine.
    """

    def _scoped(tenant_id=None, **_kwargs):
        return real_tenant_scoped_session(tenant_id, session_factory=sessionmaker)

    async def _resolve(agent_id, **_kwargs):
        return await real_resolve_tenant_for_agent(agent_id, session_factory=sessionmaker)

    monkeypatch.setattr(runtime_task_service, "tenant_scoped_session", _scoped)
    monkeypatch.setattr(runtime_task_service, "resolve_tenant_for_agent", _resolve)


async def test_create_runtime_task_sets_tenant_from_parent_agent(owner_sessionmaker, monkeypatch):
    async with owner_sessionmaker() as db:
        tid = await _mk_tenant(db)
        uid = await _mk_user(db, tid)
        aid = await _mk_agent(db, creator_id=uid, tenant_id=tid)
        await db.commit()

    _bind_accessors_to(monkeypatch, owner_sessionmaker)

    task_id = await runtime_task_service.create_runtime_task_record(
        task_id=uuid.uuid4().hex,
        task_type="trigger",
        status="running",
        parent_agent_id=aid,
    )

    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeTask, uuid.UUID(task_id))
        assert row is not None
        # Derived from the parent agent's tenant — not NULL, not invented.
        assert row.tenant_id == tid


async def test_create_runtime_task_without_parent_fails_closed(owner_sessionmaker, monkeypatch):
    from app.runtime.tenant_admission import RuntimeTenantPreconditionError

    _bind_accessors_to(monkeypatch, owner_sessionmaker)

    task_uuid = uuid.uuid4()
    with pytest.raises(RuntimeTenantPreconditionError) as exc:
        await runtime_task_service.create_runtime_task_record(
            task_id=task_uuid.hex,
            task_type="delegation",
            status="pending",
        )

    assert exc.value.reason_code == "tenant_required"
    async with owner_sessionmaker() as db:
        assert await db.get(RuntimeTask, task_uuid) is None
