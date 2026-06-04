"""Background-task tenant propagation against real PostgreSQL — §9 P0 red tests.

The P0 contract: after the initiating request finishes, a background task's
DB session must still carry the initiating tenant in ``app.current_tenant_id``.
Bare ``async_session()`` never runs ``SET LOCAL`` — that is the gap
``tenant_scoped_session`` closes for delegation ``_run``, ``run_in_background``
subagents and daemons.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from app.database import get_current_tenant_id, set_current_tenant, tenant_scoped_session

_GUC_SQL = text("SELECT current_setting('app.current_tenant_id', true)")


async def test_tenant_scoped_session_pins_explicit_tenant(owner_sessionmaker):
    tenant = str(uuid.uuid4())
    async with tenant_scoped_session(tenant, session_factory=owner_sessionmaker) as session:
        assert (await session.execute(_GUC_SQL)).scalar() == tenant


async def test_tenant_scoped_session_falls_back_to_contextvar(owner_sessionmaker):
    tenant = str(uuid.uuid4())
    set_current_tenant(tenant)
    try:
        async with tenant_scoped_session(session_factory=owner_sessionmaker) as session:
            assert (await session.execute(_GUC_SQL)).scalar() == tenant
    finally:
        set_current_tenant(None)


async def test_tenant_scoped_session_empty_tenant_fails_closed(owner_sessionmaker):
    set_current_tenant(None)
    async with tenant_scoped_session(session_factory=owner_sessionmaker) as session:
        assert (await session.execute(_GUC_SQL)).scalar() == ""


async def test_tenant_scoped_session_rejects_non_uuid(owner_sessionmaker):
    """Same injection guard as get_db(): the GUC value is interpolated, so it
    must parse as a UUID first."""
    with pytest.raises(ValueError):
        async with tenant_scoped_session("not-a-uuid'; DROP TABLE agents;--", session_factory=owner_sessionmaker):
            pass


async def test_background_task_keeps_initiating_tenant_after_request_reset(owner_sessionmaker):
    """THE P0 red test: request sets tenant → spawns background work → request
    context resets (request over) → the background session still pins the
    initiating tenant. asyncio.create_task snapshots the ContextVar; the
    session must turn that snapshot into a real SET LOCAL."""
    tenant = str(uuid.uuid4())
    set_current_tenant(tenant)

    async def background_probe() -> tuple[str | None, str | None]:
        contextvar_value = get_current_tenant_id()
        async with tenant_scoped_session(session_factory=owner_sessionmaker) as session:
            guc_value = (await session.execute(_GUC_SQL)).scalar()
        return contextvar_value, guc_value

    task = asyncio.create_task(background_probe())
    set_current_tenant(None)  # the request ends; its ContextVar resets

    contextvar_value, guc_value = await task
    assert contextvar_value == tenant
    assert guc_value == tenant
