"""§9 P11 red tests: wait_signal v2 — persistent signal-resume on real PG.

A wait_signal_step suspends the run and registers its wait; an arriving
PostgreSQL Signal (tenant + agent + thread + type matched) is consumed
exactly once and resumes the run past the wait.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.coordination import CoordinationSignal
from app.models.runtime_task import RuntimeTask
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.workflow_runtime_service import WorkflowRuntimeService
from app.services.workflow_signal_consumer import drain_signal_resumes

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _definition() -> dict:
    return {
        "name": "signal-waiter",
        "args_schema": {},
        "steps": [
            {"id": "prep", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "Prep"},
            {"id": "hold", "type": "wait_signal_step", "signal_type": "vendor_reply"},
            {"id": "after", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "After"},
        ],
    }


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-sigwait", slug=f"sw-{tid.hex[:10]}"))
    return tid


@pytest.fixture()
async def agent_id(owner_sessionmaker, tenant_id) -> uuid.UUID:
    from app.models.agent import Agent
    from app.models.user import User

    aid, uid = uuid.uuid4(), uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=uid,
                username=f"u-{uid.hex[:10]}",
                email=f"{uid.hex[:10]}@test.local",
                password_hash="x",
                display_name="Sig Wait Owner",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add(Agent(id=aid, tenant_id=tenant_id, name="sw-agent", role_description="s", creator_id=uid))
    return aid


def _leaf():
    calls: list[str] = []

    async def executor(request: LeafRequest) -> LeafOutcome:
        calls.append(request.step_id)
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    return executor, calls


async def _send_pg_signal(owner_sessionmaker, *, tenant_id, to_agent, thread_id, signal_type, content="payload"):
    signal_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            CoordinationSignal(
                id=signal_id,
                tenant_id=tenant_id,
                from_agent_id="vendor-system",
                to_agent_id=str(to_agent),
                content=content,
                signal_type=signal_type,
                thread_id=str(thread_id),
            )
        )
    return signal_id


async def test_wait_signal_step_suspends_and_registers(tenant_id, agent_id, owner_sessionmaker):
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    leaf, calls = _leaf()

    handle = await service.start_run(
        tenant_id=tenant_id, definition_data=_definition(), args={}, agent_id=agent_id, leaf_executor=leaf
    )

    assert handle.outcome.status == "suspended"
    assert "vendor_reply" in (handle.outcome.reason or "")
    assert calls == ["prep"], "the post-wait step must not run before the signal"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
    registration = (task.metadata_json or {}).get("waiting_for_signal")
    assert registration == {"step_id": "hold", "signal_type": "vendor_reply"}


async def test_pg_signal_resumes_suspended_run(tenant_id, agent_id, owner_sessionmaker):
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    leaf, _ = _leaf()
    handle = await service.start_run(
        tenant_id=tenant_id, definition_data=_definition(), args={}, agent_id=agent_id, leaf_executor=leaf
    )
    assert handle.outcome.status == "suspended"

    await _send_pg_signal(
        owner_sessionmaker,
        tenant_id=tenant_id,
        to_agent=agent_id,
        thread_id=handle.run_id,
        signal_type="vendor_reply",
        content="vendor said yes",
    )

    # A FRESH consumer (process-restart equivalent: the signal lives in PG).
    resume_leaf, resume_calls = _leaf()
    resumed = await drain_signal_resumes(leaf_executor=resume_leaf, session_factory=owner_sessionmaker)

    outcomes = {r.run_id: r.outcome.status for r in resumed}
    assert outcomes.get(handle.run_id) == "completed"
    assert resume_calls == ["after"], "prep replays from journal; only the post-wait step executes"


async def test_same_signal_resumes_only_once(tenant_id, agent_id, owner_sessionmaker):
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    leaf, _ = _leaf()
    handle = await service.start_run(
        tenant_id=tenant_id, definition_data=_definition(), args={}, agent_id=agent_id, leaf_executor=leaf
    )
    signal_id = await _send_pg_signal(
        owner_sessionmaker,
        tenant_id=tenant_id,
        to_agent=agent_id,
        thread_id=handle.run_id,
        signal_type="vendor_reply",
    )

    first = await drain_signal_resumes(leaf_executor=_leaf()[0], session_factory=owner_sessionmaker)
    assert any(r.run_id == handle.run_id and r.signal_id == signal_id for r in first)

    second = await drain_signal_resumes(leaf_executor=_leaf()[0], session_factory=owner_sessionmaker)
    assert all(r.run_id != handle.run_id for r in second), "a consumed signal must never resume twice"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        remaining = await session.scalar(select(CoordinationSignal.id).where(CoordinationSignal.id == signal_id))
    assert remaining is None, "consumption deletes the matched PG row (consume-once)"


async def test_mismatched_thread_or_type_does_not_resume(tenant_id, agent_id, owner_sessionmaker):
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    leaf, _ = _leaf()
    handle = await service.start_run(
        tenant_id=tenant_id, definition_data=_definition(), args={}, agent_id=agent_id, leaf_executor=leaf
    )

    # Wrong thread (another run's id) and wrong type — neither may match.
    await _send_pg_signal(
        owner_sessionmaker,
        tenant_id=tenant_id,
        to_agent=agent_id,
        thread_id=uuid.uuid4(),
        signal_type="vendor_reply",
    )
    await _send_pg_signal(
        owner_sessionmaker,
        tenant_id=tenant_id,
        to_agent=agent_id,
        thread_id=handle.run_id,
        signal_type="unrelated_event",
    )

    resumed = await drain_signal_resumes(leaf_executor=_leaf()[0], session_factory=owner_sessionmaker)
    assert all(r.run_id != handle.run_id for r in resumed)

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
    assert task.status == "suspended", "the run keeps waiting until ITS signal arrives"
