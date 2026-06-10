"""Runtime bootstrap survives the RLS role flip — 阶段1 make-or-break red tests.

``_resolve_runtime_config`` / ``_resolve_current_user_name`` read agents/users
by primary key to *establish* the tenant. Under the non-owner role those reads
fail-closed (empty GUC sees no tenant rows) unless wrapped in
``enter_rls_bypass`` — and a fail-closed bootstrap means **no agent can execute
at all** after the role flip. These tests seed as the owner, then resolve as
the ``app_user`` (non-owner) role and assert the real tenant comes back, not the
``tenant_resolution_error`` sentinel.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.database import Base
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.user import User
from app.runtime import invoker
from tests.integration.conftest import APP_USER


@pytest.fixture
async def complete_schema(owner_engine):
    """Fill tables ``alembic/env.py`` does not import (e.g. ``feature_flags``,
    registered only via ``app.main``) so the container schema matches what
    production builds through main.py's lifespan ``create_all``. The
    alembic-bootstrap path only sees env.py's import list — the same gap the
    ``config_revisions`` fix flagged. Re-grant freshly created tables to the
    non-owner role so RLS (not a missing GRANT) is what the tests observe."""
    async with owner_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {APP_USER}"))


async def _seed(owner_sessionmaker) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]
    async with owner_sessionmaker() as s:
        s.add(Tenant(id=tenant_id, name="T", slug=f"t-{suffix}"))
        s.add(
            User(
                id=user_id,
                username=f"u-{suffix}",
                email=f"u-{suffix}@example.test",
                password_hash="x",
                display_name="U",
                tenant_id=tenant_id,
            )
        )
        s.add(Agent(id=agent_id, name="A", creator_id=user_id, tenant_id=tenant_id))
        await s.commit()
    return agent_id, tenant_id, user_id


async def test_resolve_runtime_config_under_nonowner_role(
    complete_schema, owner_sessionmaker, app_user_sessionmaker, monkeypatch
):
    """The bootstrap returns the real tenant as the non-owner role — without the
    bypass this would be tenant_id=None + tenant_resolution_error (no agent runs)."""
    agent_id, tenant_id, _ = await _seed(owner_sessionmaker)
    monkeypatch.setattr(invoker, "async_session", app_user_sessionmaker)
    config = await invoker._resolve_runtime_config(agent_id)
    assert config.tenant_resolution_error is None
    assert str(config.tenant_id) == str(tenant_id)


async def test_resolve_current_user_name_under_nonowner_role(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    _, _, user_id = await _seed(owner_sessionmaker)
    monkeypatch.setattr(invoker, "async_session", app_user_sessionmaker)
    name = await invoker._resolve_current_user_name(user_id)
    assert name == "U"
