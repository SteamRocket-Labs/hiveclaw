"""§9 P11 red tests: persistence + tenant isolation of the signal-resume loop.

The PG row IS the durability: an unconsumed Signal survives any number of
consumer restarts; a signal belonging to another tenant can never resume
this tenant's run (RLS + explicit tenant match in the consume query).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.coordination import CoordinationSignal
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.workflow_runtime_service import WorkflowRuntimeService
from app.services.workflow_signal_consumer import drain_signal_resumes

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

from tests.integration.conftest import (  # noqa: F401, E402
    app_user_engine,
    app_user_sessionmaker,
    migrated_pg_url,
    owner_engine,
    owner_sessionmaker,
    pg_container,
)


def _definition() -> dict:
    return {
        "name": "durable-wait",
        "args_schema": {},
        "steps": [
            {"id": "hold", "type": "wait_signal_step", "signal_type": "external_ok"},
            {"id": "after", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "After"},
        ],
    }


async def _tenant_and_agent(owner_sessionmaker) -> tuple[uuid.UUID, uuid.UUID]:  # noqa: F811
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User

    tid, aid, uid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="sig-dur", slug=f"sd-{tid.hex[:10]}"))
        await session.flush()
        session.add(
            User(
                id=uid,
                username=f"u-{uid.hex[:10]}",
                email=f"{uid.hex[:10]}@test.local",
                password_hash="x",
                display_name="Durable Owner",
                tenant_id=tid,
            )
        )
        await session.flush()
        session.add(Agent(id=aid, tenant_id=tid, name="dur-agent", role_description="d", creator_id=uid))
    return tid, aid


def _leaf():
    calls: list[str] = []

    async def executor(request: LeafRequest) -> LeafOutcome:
        calls.append(request.step_id)
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    return executor, calls


async def test_unconsumed_signal_survives_consumer_restarts(owner_sessionmaker):  # noqa: F811
    tenant_id, agent_id = await _tenant_and_agent(owner_sessionmaker)
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    handle = await service.start_run(
        tenant_id=tenant_id, definition_data=_definition(), args={}, agent_id=agent_id, leaf_executor=_leaf()[0]
    )
    assert handle.outcome.status == "suspended"

    # The signal lands while NO consumer is running.
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            CoordinationSignal(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                from_agent_id="external",
                to_agent_id=str(agent_id),
                content="ok",
                signal_type="external_ok",
                thread_id=str(handle.run_id),
            )
        )

    # First consumer instance dies before draining (nothing happens). A brand
    # new instance — the restart — finds the persisted row and resumes.
    leaf, calls = _leaf()
    resumed = await drain_signal_resumes(leaf_executor=leaf, session_factory=owner_sessionmaker)
    outcomes = {r.run_id: r.outcome.status for r in resumed}

    assert outcomes.get(handle.run_id) == "completed"
    assert calls == ["after"]


async def test_foreign_tenant_signal_never_resumes_this_run(owner_sessionmaker):  # noqa: F811
    tenant_a, agent_a = await _tenant_and_agent(owner_sessionmaker)
    tenant_b, _agent_b = await _tenant_and_agent(owner_sessionmaker)

    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    handle = await service.start_run(
        tenant_id=tenant_a, definition_data=_definition(), args={}, agent_id=agent_a, leaf_executor=_leaf()[0]
    )
    assert handle.outcome.status == "suspended"

    # Tenant B emits a signal aimed (maliciously or by bug) at A's run thread.
    async with tenant_scoped_session(str(tenant_b), session_factory=owner_sessionmaker) as session:
        session.add(
            CoordinationSignal(
                id=uuid.uuid4(),
                tenant_id=tenant_b,
                from_agent_id="b-system",
                to_agent_id=str(agent_a),
                content="spoof",
                signal_type="external_ok",
                thread_id=str(handle.run_id),
            )
        )

    resumed = await drain_signal_resumes(leaf_executor=_leaf()[0], session_factory=owner_sessionmaker)
    assert all(r.run_id != handle.run_id for r in resumed), "cross-tenant signals must never match"

    # B's signal is still there (untouched), A's run still waits.
    async with tenant_scoped_session(str(tenant_b), session_factory=owner_sessionmaker) as session:
        rows = (
            (await session.execute(select(CoordinationSignal).where(CoordinationSignal.tenant_id == tenant_b)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
