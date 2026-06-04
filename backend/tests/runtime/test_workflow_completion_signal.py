"""§9 P9 red tests: workflow_completed Signal — notification ONLY (§3.3).

Readable once, never re-consumed; no wait_signal resume promise (that is
P11's persistent consumer). Uses the in-process coordination runtime + real
PG run records.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents.coordination import coordination_runtime
from app.database import tenant_scoped_session
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.workflow_runtime_service import WorkflowRuntimeService

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _definition() -> dict:
    return {
        "name": "signalled",
        "args_schema": {},
        "steps": [
            {"id": "only", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "Do"},
        ],
    }


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-sig", slug=f"ws-{tid.hex[:10]}"))
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
                display_name="Sig Owner",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add(Agent(id=aid, tenant_id=tenant_id, name="sig-agent", role_description="s", creator_id=uid))
    return aid


async def test_completed_run_emits_consume_once_signal(tenant_id, agent_id, owner_sessionmaker):
    coordination_runtime.reset()
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)

    async def leaf(request: LeafRequest) -> LeafOutcome:
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    handle = await service.start_run(
        tenant_id=tenant_id, definition_data=_definition(), args={}, agent_id=agent_id, leaf_executor=leaf
    )
    assert handle.outcome.status == "completed"

    signals = coordination_runtime.consume_signals(
        str(agent_id), thread_id=str(handle.run_id), signal_type="workflow_completed"
    )
    assert len(signals) == 1
    assert signals[0].signal_type == "workflow_completed"
    assert str(handle.run_id) in signals[0].content

    again = coordination_runtime.consume_signals(
        str(agent_id), thread_id=str(handle.run_id), signal_type="workflow_completed"
    )
    assert again == [], "the completion signal is read-once — never re-consumed"


async def test_failed_run_emits_no_completion_signal(tenant_id, agent_id, owner_sessionmaker):
    coordination_runtime.reset()
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)

    async def failing_leaf(request: LeafRequest) -> LeafOutcome:
        return LeafOutcome(ok=False, error="boom")

    handle = await service.start_run(
        tenant_id=tenant_id, definition_data=_definition(), args={}, agent_id=agent_id, leaf_executor=failing_leaf
    )
    assert handle.outcome.status == "failed"

    signals = coordination_runtime.consume_signals(
        str(agent_id), thread_id=str(handle.run_id), signal_type="workflow_completed"
    )
    assert signals == []
