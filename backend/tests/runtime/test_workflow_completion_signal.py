"""§9 P9 red tests: workflow_completed Signal — notification ONLY (§3.3).

Readable once, never re-consumed; no wait_signal resume promise (that is
P11's persistent consumer). Uses the in-process coordination runtime + real
PG run records.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from app.database import tenant_scoped_session
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.models.coordination import CoordinationSignal
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
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)

    async def leaf(request: LeafRequest) -> LeafOutcome:
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    handle = await service.start_run(
        tenant_id=tenant_id, definition_data=_definition(), args={}, agent_id=agent_id, leaf_executor=leaf
    )
    assert handle.outcome.status == "completed"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        signals = (
            (
                await session.execute(
                    select(CoordinationSignal).where(
                        CoordinationSignal.to_agent_id == str(agent_id),
                        CoordinationSignal.thread_id == str(handle.run_id),
                        CoordinationSignal.signal_type == "workflow_completed",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(signals) == 1
    assert signals[0].signal_type == "workflow_completed"
    assert str(handle.run_id) in signals[0].content

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        await session.execute(delete(CoordinationSignal).where(CoordinationSignal.id == signals[0].id))
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        again = (
            (await session.execute(select(CoordinationSignal).where(CoordinationSignal.id == signals[0].id)))
            .scalars()
            .all()
        )
    assert again == [], "the completion signal is read-once — never re-consumed"


async def test_completed_run_replay_does_not_emit_completion_side_effects_twice(
    tenant_id, agent_id, owner_sessionmaker, monkeypatch
):
    from sqlalchemy import select

    from app.models.runtime_task import RuntimeTask
    from app.services import workflow_runtime_service as module

    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    delivered: list[dict] = []

    async def fake_send_text(**kwargs):
        delivered.append(kwargs)
        return None

    monkeypatch.setattr(module.ChannelDeliveryService, "send_text", fake_send_text)

    async def leaf(request: LeafRequest) -> LeafOutcome:
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={},
        agent_id=agent_id,
        leaf_executor=leaf,
        delivery_target={"channel": "web", "username": "owner"},
    )
    assert handle.outcome.status == "completed"
    assert delivered == []
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        signals = (
            (
                await session.execute(
                    select(CoordinationSignal).where(
                        CoordinationSignal.thread_id == str(handle.run_id),
                        CoordinationSignal.signal_type == "workflow_completed",
                    )
                )
            )
            .scalars()
            .all()
        )
        deliveries = (
            (
                await session.execute(
                    select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == handle.run_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(signals) == 1
    assert len(deliveries) == 1

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
        task.status = "running"

    async def exploding_leaf(request: LeafRequest) -> LeafOutcome:
        raise AssertionError("done workflow steps must replay from journal, not re-execute leaves")

    replay = await service.resume_run(handle.run_id, tenant_id=tenant_id, leaf_executor=exploding_leaf)

    assert replay.status == "completed"
    assert delivered == []
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        signal_count = len(
            (
                (
                    await session.execute(
                        select(CoordinationSignal).where(
                            CoordinationSignal.thread_id == str(handle.run_id),
                            CoordinationSignal.signal_type == "workflow_completed",
                        )
                    )
                )
                .scalars()
                .all()
            )
        )
        delivery_count = len(
            (
                (
                    await session.execute(
                        select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == handle.run_id)
                    )
                )
                .scalars()
                .all()
            )
        )
    assert signal_count == 1
    assert delivery_count == 1


async def test_failed_run_emits_no_completion_signal(tenant_id, agent_id, owner_sessionmaker):
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)

    async def failing_leaf(request: LeafRequest) -> LeafOutcome:
        return LeafOutcome(ok=False, error="boom")

    handle = await service.start_run(
        tenant_id=tenant_id, definition_data=_definition(), args={}, agent_id=agent_id, leaf_executor=failing_leaf
    )
    assert handle.outcome.status == "failed"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        signals = (
            (
                await session.execute(
                    select(CoordinationSignal).where(
                        CoordinationSignal.thread_id == str(handle.run_id),
                        CoordinationSignal.signal_type == "workflow_completed",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert signals == []
