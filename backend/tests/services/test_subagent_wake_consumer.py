from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.coordination import CoordinationSignal
from app.models.runtime_task import RuntimeTask

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="subagent-wake", slug=f"sw-{tid.hex[:10]}"))
    return tid


async def _send_completion_signal(owner_sessionmaker, tenant_id: uuid.UUID, parent_agent_id: uuid.UUID) -> uuid.UUID:
    signal_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            CoordinationSignal(
                id=signal_id,
                tenant_id=tenant_id,
                from_agent_id="subagent:researcher",
                to_agent_id=str(parent_agent_id),
                content="background result",
                signal_type="subagent_completed",
                thread_id="trace-1",
            )
        )
    return signal_id


async def _signal_count(owner_sessionmaker, tenant_id: uuid.UUID) -> int:
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        return len((await session.execute(select(CoordinationSignal))).scalars().all())


async def test_subagent_completion_wakes_idle_parent_once(owner_sessionmaker, tenant_id):
    from app.services.subagent_wake_consumer import SubagentWakeRequest, drain_subagent_completion_wakes

    parent_agent_id = uuid.uuid4()
    signal_id = await _send_completion_signal(owner_sessionmaker, tenant_id, parent_agent_id)
    invoked: list[SubagentWakeRequest] = []

    async def invoke_parent(request: SubagentWakeRequest) -> str:
        invoked.append(request)
        return "parent resumed"

    result = await drain_subagent_completion_wakes(
        session_factory=owner_sessionmaker,
        invoke_parent=invoke_parent,
    )
    again = await drain_subagent_completion_wakes(
        session_factory=owner_sessionmaker,
        invoke_parent=invoke_parent,
    )

    assert len(result) == 1
    assert result[0].signal_id == signal_id
    assert result[0].status == "woken"
    assert len(invoked) == 1
    assert invoked[0].parent_agent_id == parent_agent_id
    assert invoked[0].tenant_id == tenant_id
    assert "background result" in invoked[0].content
    assert again == []
    assert await _signal_count(owner_sessionmaker, tenant_id) == 0


async def test_subagent_completion_does_not_wake_parent_with_active_run(owner_sessionmaker, tenant_id):
    from app.services.subagent_wake_consumer import SubagentWakeRequest, drain_subagent_completion_wakes

    parent_agent_id = uuid.uuid4()
    await _send_completion_signal(owner_sessionmaker, tenant_id, parent_agent_id)
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            RuntimeTask(
                id=uuid.uuid4(),
                task_type="web_chat_turn",
                status="running",
                parent_agent_id=parent_agent_id,
            )
        )
    invoked: list[SubagentWakeRequest] = []

    async def invoke_parent(request: SubagentWakeRequest) -> str:
        invoked.append(request)
        return "should not run"

    result = await drain_subagent_completion_wakes(
        session_factory=owner_sessionmaker,
        invoke_parent=invoke_parent,
    )

    assert result == []
    assert invoked == []
    assert await _signal_count(owner_sessionmaker, tenant_id) == 1
