from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import delete, select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.participant import Participant
from app.models.runtime_task import RuntimeTask
from app.models.workflow_completion_outbox import WorkflowCompletionOutbox
from app.services.workflow_completion_outbox import (
    WorkflowCompletionIntent,
    WorkflowCompletionOutboxService,
    enqueue_workflow_completion,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture()
async def workflow_completion_run(owner_sessionmaker):
    from app.models.agent import Agent
    from app.models.audit import AuditLog
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tenant_id, name="workflow-outbox", slug=f"wo-{tenant_id.hex[:10]}"))
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=user_id,
                username=f"wo-{user_id.hex[:10]}",
                email=f"wo-{user_id.hex[:10]}@test.local",
                password_hash="x",
                display_name="Workflow Outbox Owner",
                tenant_id=tenant_id,
                role="platform_admin",
            )
        )
        await session.flush()
        agent = Agent(id=agent_id, tenant_id=tenant_id, name="outbox-agent", creator_id=user_id)
        session.add(agent)
        await session.flush()
        participant_id = agent.participant_id
        assert participant_id is not None
        session.add(
            RuntimeTask(
                id=run_id,
                task_type="workflow",
                tenant_id=tenant_id,
                status="completed",
                parent_agent_id=agent_id,
                root_user_id=user_id,
                root_runtime_task_id=run_id,
                metadata_json={
                    "definition_hash": "hash",
                    "test_fixture": "workflow_completion_outbox",
                },
                result_summary="Workflow finished",
            )
        )
        outbox_id = await enqueue_workflow_completion(
            session,
            WorkflowCompletionIntent(
                tenant_id=tenant_id,
                run_id=run_id,
                agent_id=agent_id,
                terminal_status="completed",
            ),
        )
        await session.flush()
        outbox = await session.get(WorkflowCompletionOutbox, outbox_id)
        assert outbox is not None
        outbox.available_at = datetime(2000, 1, 1, tzinfo=UTC)
    yield tenant_id, agent_id, run_id

    async with owner_sessionmaker() as session:
        await session.execute(delete(AuditLog).where(AuditLog.tenant_id == tenant_id))
        await session.execute(delete(WorkflowCompletionOutbox).where(WorkflowCompletionOutbox.tenant_id == tenant_id))
        await session.execute(delete(RuntimeTask).where(RuntimeTask.tenant_id == tenant_id))
        await session.execute(delete(Agent).where(Agent.id == agent_id))
        await session.execute(
            delete(Participant).where(
                Participant.id == participant_id,
                Participant.type == "agent",
            )
        )
        await session.execute(delete(User).where(User.id == user_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.commit()
    async with owner_sessionmaker() as session:
        assert await session.get(Participant, participant_id) is None
        assert await session.get(RuntimeTask, run_id) is None
        assert (
            await session.execute(
                select(WorkflowCompletionOutbox).where(
                    WorkflowCompletionOutbox.tenant_id == tenant_id,
                )
            )
        ).scalars().all() == []


class _IdempotentGateway:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.attempts: list[uuid.UUID] = []
        self.delivered_ids: set[uuid.UUID] = set()

    async def send_signal(self, **kwargs):
        signal_id = uuid.UUID(str(kwargs["signal_id"]))
        self.attempts.append(signal_id)
        if self.fail_first and len(self.attempts) == 1:
            raise RuntimeError("coordination backend unavailable")
        self.delivered_ids.add(signal_id)
        return type("Signal", (), {"id": str(signal_id)})()


def _gateway_scope(gateway):
    @contextlib.asynccontextmanager
    async def scope(*_args, **_kwargs):
        yield gateway

    return scope


async def test_claim_then_crash_restarts_and_replays_signal_exactly_once(
    workflow_completion_run,
    owner_sessionmaker,
    monkeypatch,
):
    from app.services import workflow_completion_outbox as module

    tenant_id, _agent_id, run_id = workflow_completion_run
    gateway = _IdempotentGateway()
    monkeypatch.setattr(module, "gateway_scope", _gateway_scope(gateway))
    worker_a = WorkflowCompletionOutboxService(session_factory=owner_sessionmaker, lease_seconds=1)
    claimed = await worker_a.claim_batch(worker_id="worker-a", limit=1)
    assert len(claimed) == 1
    assert claimed[0].run_id == run_id

    # Side effect succeeds, then the process dies before the outbox ack.
    await worker_a._deliver(claimed[0])
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        row = (
            await session.execute(select(WorkflowCompletionOutbox).where(WorkflowCompletionOutbox.run_id == run_id))
        ).scalar_one()
        row.locked_at = datetime.now(UTC) - timedelta(seconds=5)

    worker_b = WorkflowCompletionOutboxService(
        session_factory=owner_sessionmaker,
        lease_seconds=1,
        retry_base_seconds=0,
    )
    result = await worker_b.drain_once(worker_id="worker-b", limit=1)

    assert result == {"claimed": 1, "delivered": 1, "retried": 0, "dead_lettered": 0}
    assert len(gateway.attempts) == 2
    assert len(gateway.delivered_ids) == 1, "stable signal_id makes post-send crash replay idempotent"


async def test_send_failure_retries_after_restart_and_duplicate_pumps_are_noops(
    workflow_completion_run,
    owner_sessionmaker,
    monkeypatch,
):
    from app.services import workflow_completion_outbox as module

    tenant_id, _agent_id, run_id = workflow_completion_run
    gateway = _IdempotentGateway(fail_first=True)
    monkeypatch.setattr(module, "gateway_scope", _gateway_scope(gateway))
    worker_a = WorkflowCompletionOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=3,
    )

    first = await worker_a.drain_once(worker_id="worker-a", limit=1)
    assert first == {"claimed": 1, "delivered": 0, "retried": 1, "dead_lettered": 0}
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        target_row = (
            await session.execute(
                select(WorkflowCompletionOutbox)
                .where(
                    WorkflowCompletionOutbox.tenant_id == tenant_id,
                    WorkflowCompletionOutbox.run_id == run_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        target_outbox_id = target_row.id
        # _mark_failed schedules from the current clock. Reassert this
        # fixture's queue position before testing its retry; the production
        # worker remains global and may have unrelated older backlog.
        target_row.available_at = datetime(1990, 1, 1, tzinfo=UTC)

    worker_b = WorkflowCompletionOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=3,
    )
    second = await worker_b.drain_once(worker_id="worker-b", limit=1)
    # The worker is global: another queued tenant/run may be advanced here.
    # Exactly-once is asserted against this fixture's stable outbox id below.
    await worker_b.drain_once(worker_id="worker-b", limit=1)

    assert second == {"claimed": 1, "delivered": 1, "retried": 0, "dead_lettered": 0}
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        row = (
            await session.execute(select(WorkflowCompletionOutbox).where(WorkflowCompletionOutbox.run_id == run_id))
        ).scalar_one()
    assert row.status == "delivered"
    assert row.attempt_count == 2
    assert row.id == target_outbox_id
    assert gateway.attempts.count(target_outbox_id) == 2
    assert target_outbox_id in gateway.delivered_ids


@pytest.mark.parametrize("invalid_authority", ["deactivated", "deleted", "source_not_completed"])
async def test_automatic_delivery_dead_letters_invalid_live_authority_without_sending(
    workflow_completion_run,
    owner_sessionmaker,
    monkeypatch,
    invalid_authority,
):
    from app.services import workflow_completion_outbox as module

    tenant_id, agent_id, run_id = workflow_completion_run
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        if invalid_authority == "source_not_completed":
            task = await session.get(RuntimeTask, run_id, with_for_update=True)
            assert task is not None
            task.status = "failed"
        else:
            agent = await session.get(Agent, agent_id, with_for_update=True)
            assert agent is not None
            setattr(
                agent,
                "deactivated_at" if invalid_authority == "deactivated" else "deleted_at",
                datetime.now(UTC),
            )

    gateway = _IdempotentGateway()
    monkeypatch.setattr(module, "gateway_scope", _gateway_scope(gateway))
    service = WorkflowCompletionOutboxService(session_factory=owner_sessionmaker)

    result = await service.drain_once(worker_id=f"authority-check-{invalid_authority}", limit=1)

    assert result == {"claimed": 1, "delivered": 0, "retried": 0, "dead_lettered": 1}
    assert gateway.attempts == []
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        row = (
            await session.execute(
                select(WorkflowCompletionOutbox).where(
                    WorkflowCompletionOutbox.tenant_id == tenant_id,
                    WorkflowCompletionOutbox.run_id == run_id,
                )
            )
        ).scalar_one()
    assert row.status == "dead_letter"
    assert row.locked_by is None
    assert row.locked_at is None
    assert "authority" in str(row.last_error).lower()


async def test_repair_pump_backfills_missing_terminal_intent_once(
    workflow_completion_run,
    owner_sessionmaker,
):
    tenant_id, _agent_id, run_id = workflow_completion_run
    service = WorkflowCompletionOutboxService(session_factory=owner_sessionmaker)
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        row = (
            await session.execute(select(WorkflowCompletionOutbox).where(WorkflowCompletionOutbox.run_id == run_id))
        ).scalar_one()
        await session.delete(row)

    assert await service.reconcile_terminal_runs_once(limit=100, tenant_id=tenant_id) == 1
    assert await service.reconcile_terminal_runs_once(limit=100, tenant_id=tenant_id) == 0

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        rows = list(
            (await session.execute(select(WorkflowCompletionOutbox).where(WorkflowCompletionOutbox.run_id == run_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1


async def test_repair_pump_advances_oldest_gaps_under_continuous_new_traffic(
    workflow_completion_run,
    owner_sessionmaker,
):
    tenant_id, agent_id, fixture_run_id = workflow_completion_run
    service = WorkflowCompletionOutboxService(session_factory=owner_sessionmaker)
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        fixture_task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == fixture_run_id))).scalar_one()
        root_user_id = fixture_task.root_user_id
    assert root_user_id is not None

    tie_time = datetime(2026, 1, 1, tzinfo=UTC)
    old_run_ids = [
        uuid.UUID("00000000-0000-4000-8000-000000000001"),
        uuid.UUID("00000000-0000-4000-8000-000000000002"),
    ]
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        for run_id in reversed(old_run_ids):
            session.add(
                RuntimeTask(
                    id=run_id,
                    task_type="workflow",
                    tenant_id=tenant_id,
                    status="completed",
                    parent_agent_id=agent_id,
                    root_user_id=root_user_id,
                    root_runtime_task_id=run_id,
                    metadata_json={"test_fixture": "workflow_completion_outbox", "definition_hash": "old-gap"},
                    result_summary="Old Workflow gap",
                    created_at=tie_time,
                    completed_at=tie_time,
                )
            )

    repaired_old_ids: list[uuid.UUID] = []
    for tick in range(2):
        new_run_id = uuid.uuid4()
        current = datetime(2026, 1, 2, tick, tzinfo=UTC)
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            session.add(
                RuntimeTask(
                    id=new_run_id,
                    task_type="workflow",
                    tenant_id=tenant_id,
                    status="completed",
                    parent_agent_id=agent_id,
                    root_user_id=root_user_id,
                    root_runtime_task_id=new_run_id,
                    metadata_json={"test_fixture": "workflow_completion_outbox", "definition_hash": "new-gap"},
                    result_summary="New Workflow gap",
                    created_at=current,
                    completed_at=current,
                )
            )
        assert await service.reconcile_terminal_runs_once(limit=1, tenant_id=tenant_id) == 1
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            repaired_old_ids = list(
                (
                    await session.execute(
                        select(WorkflowCompletionOutbox.run_id)
                        .where(WorkflowCompletionOutbox.run_id.in_(old_run_ids))
                        .order_by(WorkflowCompletionOutbox.run_id)
                    )
                )
                .scalars()
                .all()
            )
        assert repaired_old_ids == old_run_ids[: tick + 1]


async def test_dead_letter_operator_retry_reuses_stable_signal_without_rerunning_workflow(
    workflow_completion_run,
    owner_sessionmaker,
    monkeypatch,
):
    from app.models.audit import AuditLog
    from app.services import workflow_completion_outbox as module

    tenant_id, _agent_id, run_id = workflow_completion_run
    gateway = _IdempotentGateway(fail_first=True)
    monkeypatch.setattr(module, "gateway_scope", _gateway_scope(gateway))
    service = WorkflowCompletionOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=1,
    )
    first = await service.drain_once(worker_id="worker-a", limit=1)
    assert first == {"claimed": 1, "delivered": 0, "retried": 0, "dead_lettered": 1}

    dead_letters = await service.list_dead_letters(tenant_id=tenant_id, limit=20)
    assert len(dead_letters) == 1
    assert dead_letters[0]["run_id"] == str(run_id)
    assert dead_letters[0]["delivery_only"] is True
    assert dead_letters[0]["does_not_rerun_execution"] is True
    outbox_id = uuid.UUID(dead_letters[0]["id"])
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        actor_user_id = task.root_user_id
    assert actor_user_id is not None

    retried = await service.retry_dead_letter(
        tenant_id=tenant_id,
        outbox_id=outbox_id,
        actor_user_id=actor_user_id,
        reason="coordination backend recovered",
    )
    assert retried["id"] == str(outbox_id)
    assert retried["status"] == "pending"
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        retried_row = (
            await session.execute(
                select(WorkflowCompletionOutbox).where(WorkflowCompletionOutbox.id == outbox_id).with_for_update()
            )
        ).scalar_one()
        retried_row.available_at = datetime(2000, 1, 1, tzinfo=UTC)
    second = await service.drain_once(worker_id="worker-b", limit=1)
    assert second == {"claimed": 1, "delivered": 1, "retried": 0, "dead_lettered": 0}
    assert len(gateway.delivered_ids) == 1
    assert gateway.delivered_ids == {outbox_id}

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        row = (
            await session.execute(select(WorkflowCompletionOutbox).where(WorkflowCompletionOutbox.id == outbox_id))
        ).scalar_one()
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.tenant_id == tenant_id,
                    AuditLog.action == "workflow_completion_delivery_retry",
                )
            )
        ).scalar_one()
    assert task.status == "completed", "delivery retry must never re-execute Workflow"
    assert row.status == "delivered"
    assert row.attempt_count == 1
    assert audit.details["outbox_id"] == str(outbox_id)
    assert audit.details["delivery_only"] is True


async def test_dead_letter_payload_exposes_live_target_authority_and_retry_fails_closed(
    workflow_completion_run,
    owner_sessionmaker,
):
    tenant_id, agent_id, run_id = workflow_completion_run
    service = WorkflowCompletionOutboxService(session_factory=owner_sessionmaker)
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        row = (
            await session.execute(
                select(WorkflowCompletionOutbox).where(WorkflowCompletionOutbox.run_id == run_id).with_for_update()
            )
        ).scalar_one()
        row.status = "dead_letter"
        actor_user_id = (
            await session.execute(select(RuntimeTask.root_user_id).where(RuntimeTask.id == run_id))
        ).scalar_one()
    assert actor_user_id is not None

    [listed] = await service.list_dead_letters(tenant_id=tenant_id, limit=20)
    assert listed["authority_snapshot"] == {
        "valid": True,
        "tenant_id": str(tenant_id),
        "agent_id": str(agent_id),
    }
    assert listed["retryable"] is True

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = await session.get(Agent, agent_id, with_for_update=True)
        assert agent is not None
        agent.deactivated_at = datetime.now(UTC)

    [invalid] = await service.list_dead_letters(tenant_id=tenant_id, limit=20)
    assert invalid["authority_snapshot"]["valid"] is False
    assert invalid["retryable"] is False
    with pytest.raises(PermissionError, match="target authority"):
        await service.retry_dead_letter(
            tenant_id=tenant_id,
            outbox_id=invalid["delivery_id"],
            actor_user_id=actor_user_id,
            reason="verified repaired target authority",
        )


@pytest.mark.parametrize("reason", ["        ", "  short  "])
async def test_workflow_dead_letter_retry_rejects_blank_or_short_trimmed_reason(
    workflow_completion_run,
    owner_sessionmaker,
    reason,
):
    tenant_id, _agent_id, run_id = workflow_completion_run
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        row = (
            await session.execute(
                select(WorkflowCompletionOutbox).where(WorkflowCompletionOutbox.run_id == run_id).with_for_update()
            )
        ).scalar_one()
        row.status = "dead_letter"
        actor_user_id = (
            await session.execute(select(RuntimeTask.root_user_id).where(RuntimeTask.id == run_id))
        ).scalar_one()
    assert actor_user_id is not None

    with pytest.raises(ValueError, match="at least 8"):
        await WorkflowCompletionOutboxService(session_factory=owner_sessionmaker).retry_dead_letter(
            tenant_id=tenant_id,
            outbox_id=row.id,
            actor_user_id=actor_user_id,
            reason=reason,
        )
