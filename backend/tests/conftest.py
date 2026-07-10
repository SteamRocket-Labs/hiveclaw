from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@pytest.fixture(autouse=True)
def _reset_global_engine_pools():
    """Drop the module-level engines' pooled connections after every test.

    pytest-asyncio gives each test its own event loop, but ``app.database``'s
    engines are process-global with pooled connections bound to whichever loop
    first used them. A later test that reaches the global engine (any code
    path not wired through an injected session factory) then explodes with
    ``RuntimeError: ... attached to a different loop`` — an order-dependent
    failure class (e.g. test_skill_distiller poisoning the subagent spawn
    path's team-contract lookup).

    ``dispose(close=False)`` on the sync facade discards the pool without
    awaiting connection close — the loop-safe disposal SQLAlchemy documents
    for multi-loop asyncio use; asyncpg terminates the dropped connections on
    garbage collection.
    """
    yield
    from app import database

    database.engine.sync_engine.dispose(close=False)
    if database.schema_engine is not database.engine:
        database.schema_engine.sync_engine.dispose(close=False)


@pytest.fixture
async def workflow_principals(owner_sessionmaker, tenant_id):
    """Seed real FK-backed actors for workflow asset revision/audit tests."""

    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.user import User

    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=user_id,
                username=f"workflow-{user_id.hex[:10]}",
                email=f"workflow-{user_id.hex[:10]}@test.local",
                password_hash="x",
                display_name="Workflow Owner",
                tenant_id=tenant_id,
                role="org_admin",
            )
        )
        await session.flush()
        session.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="workflow-agent",
                role_description="Workflow test actor",
                creator_id=user_id,
                owner_user_id=user_id,
                status="idle",
            )
        )
    return SimpleNamespace(user_id=user_id, agent_id=agent_id)
