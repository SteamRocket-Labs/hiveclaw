"""§9 P9 red tests: workflow_completed Signal — notification ONLY (§3.3).

Readable once, never re-consumed; no wait_signal resume promise (that is
P11's persistent consumer). Uses the in-process coordination runtime + real
PG run records.
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from sqlalchemy import delete, select

from app.agents.coordination import coordination_runtime
from app.database import tenant_scoped_session
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.workflow_runtime_service import WorkflowRuntimeService
from app.services.workflow_completion_outbox import WorkflowCompletionOutboxService

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture(autouse=True)
def _use_in_process_coordination_backend(monkeypatch):
    class _Settings:
        COORDINATION_BACKEND = "memory"

    monkeypatch.setattr("app.agents.coordination_wiring.get_settings", lambda: _Settings())


def _definition() -> dict:
    return {
        "name": "signalled",
        "args_schema": {},
        "steps": [
            {"id": "only", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "Do"},
        ],
    }


async def _prioritize_target_completion(
    owner_sessionmaker,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
) -> None:
    from app.models.workflow_completion_outbox import WorkflowCompletionOutbox

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        row = (
            await session.execute(
                select(WorkflowCompletionOutbox).where(
                    WorkflowCompletionOutbox.tenant_id == tenant_id,
                    WorkflowCompletionOutbox.run_id == run_id,
                )
            )
        ).scalar_one()
        row.available_at = datetime(1990, 1, 1, tzinfo=UTC)


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.agent import Agent
    from app.models.audit import AuditLog, ChatMessage
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.models.workflow_completion_outbox import WorkflowCompletionOutbox

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-sig", slug=f"ws-{tid.hex[:10]}"))
    yield tid

    async with owner_sessionmaker() as session:
        await session.execute(delete(AuditLog).where(AuditLog.tenant_id == tid))
        await session.execute(delete(WorkflowCompletionOutbox).where(WorkflowCompletionOutbox.tenant_id == tid))
        await session.execute(delete(ChatTranscriptEvent).where(ChatTranscriptEvent.tenant_id == tid))
        await session.execute(delete(ChatMessage).where(ChatMessage.tenant_id == tid))
        await session.execute(delete(ChatSession).where(ChatSession.tenant_id == tid))
        await session.execute(delete(RuntimeTask).where(RuntimeTask.tenant_id == tid))
        await session.execute(delete(RuntimeBudgetRun).where(RuntimeBudgetRun.tenant_id == tid))
        await session.execute(delete(Agent).where(Agent.tenant_id == tid))
        await session.execute(delete(User).where(User.tenant_id == tid))
        await session.execute(delete(Tenant).where(Tenant.id == tid))
        await session.commit()


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
    from app.models.audit import AuditLog

    coordination_runtime.reset()
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)

    async def leaf(request: LeafRequest) -> LeafOutcome:
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    handle = await service.start_run(
        tenant_id=tenant_id, definition_data=_definition(), args={}, agent_id=agent_id, leaf_executor=leaf
    )
    assert handle.outcome.status == "completed"
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        audit_actions = set(
            (
                await session.execute(
                    select(AuditLog.action).where(
                        AuditLog.tenant_id == tenant_id,
                        AuditLog.agent_id == agent_id,
                        AuditLog.details["run_id"].astext == str(handle.run_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {"workflow_run_started", "workflow_run_completed"} <= audit_actions
    await _prioritize_target_completion(owner_sessionmaker, tenant_id=tenant_id, run_id=handle.run_id)
    pump = WorkflowCompletionOutboxService(session_factory=owner_sessionmaker)
    assert (await pump.drain_once(worker_id="signal-worker", limit=1))["delivered"] == 1

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


async def test_completed_run_replay_does_not_emit_completion_side_effects_twice(
    tenant_id, agent_id, owner_sessionmaker
):
    from sqlalchemy import select

    from app.models.runtime_task import RuntimeTask

    coordination_runtime.reset()
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)

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
    await _prioritize_target_completion(owner_sessionmaker, tenant_id=tenant_id, run_id=handle.run_id)
    pump = WorkflowCompletionOutboxService(session_factory=owner_sessionmaker)
    assert (await pump.drain_once(worker_id="signal-worker", limit=1))["delivered"] == 1
    assert (
        len(
            coordination_runtime.consume_signals(
                str(agent_id), thread_id=str(handle.run_id), signal_type="workflow_completed"
            )
        )
        == 1
    )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
        task.status = "running"

    async def exploding_leaf(request: LeafRequest) -> LeafOutcome:
        raise AssertionError("done workflow steps must replay from journal, not re-execute leaves")

    replay = await service.resume_run(handle.run_id, tenant_id=tenant_id, leaf_executor=exploding_leaf)

    assert replay.status == "completed"
    from app.models.workflow_completion_outbox import WorkflowCompletionOutbox

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        target_rows = list(
            (
                await session.execute(
                    select(WorkflowCompletionOutbox).where(
                        WorkflowCompletionOutbox.tenant_id == tenant_id,
                        WorkflowCompletionOutbox.run_id == handle.run_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(target_rows) == 1
    assert target_rows[0].status == "delivered"
    assert target_rows[0].attempt_count == 1
    assert (
        coordination_runtime.consume_signals(
            str(agent_id), thread_id=str(handle.run_id), signal_type="workflow_completed"
        )
        == []
    )


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
