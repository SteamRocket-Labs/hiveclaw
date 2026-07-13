from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
import sys
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.dialects import postgresql

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember
from app.models.audit import ApprovalRequest, AuditLog, ChatMessage
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.plan_request import AgentPlanRequest
from app.models.participant import Participant
from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.services.runtime_notification_outbox import (
    CompletionNotification,
    RuntimeNotificationOutboxService,
    enqueue_completion_notification,
    list_runtime_notification_delivery_reconciliations,
    retry_runtime_notification_delivery,
)


async def _seed_parent_session(owner_sessionmaker, *, source_channel: str = "web"):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Outbox Tenant", slug=f"outbox-{tenant_id.hex[:12]}"))
        db.add(
            User(
                id=user_id,
                username=f"outbox-{user_id.hex[:10]}",
                email=f"{user_id.hex[:12]}@outbox.test",
                password_hash="x",
                display_name="Outbox Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Outbox Agent",
                role_description="deliver completion",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await db.flush()
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                title="Parent Session",
                source_channel=source_channel,
                session_kind="human_chat",
                actor_type="user",
                runtime_source="web_chat",
                visibility_scope="direct_user",
                listed_surface="chat",
                delivery_target_json=(
                    {"channel": source_channel, "user_id": "external-owner"} if source_channel != "web" else None
                ),
            )
        )
        await db.commit()
    return tenant_id, user_id, agent_id, session_id


# A deliberately untracked alias used by the cleanup-boundary test. It
# represents facts owned by another scope; cleanup for the owned tenant must
# never discover or delete it through monkeypatch bookkeeping.
_seed_external_scope_sentinel = _seed_parent_session


async def _delete_outbox_test_tenants(owner_sessionmaker, tenant_ids: set[uuid.UUID]) -> None:
    """Delete only facts owned by this test's explicitly tracked tenants."""

    if not tenant_ids:
        return
    async with owner_sessionmaker() as db:
        team_ids = select(AgentTeam.id).where(AgentTeam.tenant_id.in_(tenant_ids))
        participant_ids = set(
            (await db.execute(select(Agent.participant_id).where(Agent.tenant_id.in_(tenant_ids)))).scalars().all()
        )
        await db.execute(delete(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.tenant_id.in_(tenant_ids)))
        await db.execute(delete(ChatTranscriptEvent).where(ChatTranscriptEvent.tenant_id.in_(tenant_ids)))
        await db.execute(delete(ChatMessage).where(ChatMessage.tenant_id.in_(tenant_ids)))
        await db.execute(delete(AuditLog).where(AuditLog.tenant_id.in_(tenant_ids)))
        await db.execute(delete(ApprovalRequest).where(ApprovalRequest.tenant_id.in_(tenant_ids)))
        await db.execute(delete(AgentTeamEvent).where(AgentTeamEvent.team_id.in_(team_ids)))
        await db.execute(delete(AgentTeamMember).where(AgentTeamMember.team_id.in_(team_ids)))
        await db.execute(delete(AgentTeam).where(AgentTeam.tenant_id.in_(tenant_ids)))
        await db.execute(delete(AgentPlanRequest).where(AgentPlanRequest.tenant_id.in_(tenant_ids)))
        await db.execute(delete(RuntimeBudgetEvent).where(RuntimeBudgetEvent.tenant_id.in_(tenant_ids)))
        await db.execute(delete(ChatSession).where(ChatSession.tenant_id.in_(tenant_ids)))
        await db.execute(delete(RuntimeTask).where(RuntimeTask.tenant_id.in_(tenant_ids)))
        await db.execute(delete(RuntimeBudgetRun).where(RuntimeBudgetRun.tenant_id.in_(tenant_ids)))
        await db.execute(delete(Agent).where(Agent.tenant_id.in_(tenant_ids)))
        if participant_ids:
            await db.execute(
                delete(Participant).where(
                    Participant.id.in_(participant_ids),
                    Participant.type == "agent",
                )
            )
        await db.execute(delete(User).where(User.tenant_id.in_(tenant_ids)))
        await db.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
        await db.commit()


@pytest.fixture(autouse=True)
async def _cleanup_outbox_test_tenant_scope(owner_sessionmaker, monkeypatch):
    """Track `_seed_parent_session` tenants and tear down only those rows."""

    async with owner_sessionmaker() as db:
        baseline_tenant_ids = set((await db.execute(select(Tenant.id))).scalars().all())
        baseline_task_ids = set((await db.execute(select(RuntimeTask.id))).scalars().all())
        baseline_outbox_ids = set((await db.execute(select(RuntimeNotificationOutbox.id))).scalars().all())
        baseline_participant_ids = set((await db.execute(select(Participant.id))).scalars().all())

    tracked_tenant_ids: set[uuid.UUID] = set()
    seed_parent_session = _seed_parent_session

    async def tracked_seed_parent_session(*args, **kwargs):
        authority = await seed_parent_session(*args, **kwargs)
        tracked_tenant_ids.add(authority[0])
        return authority

    monkeypatch.setattr(sys.modules[__name__], "_seed_parent_session", tracked_seed_parent_session)
    try:
        yield
    finally:
        async with owner_sessionmaker() as db:
            current_tenant_ids = set((await db.execute(select(Tenant.id))).scalars().all())
        new_tenant_ids = current_tenant_ids - baseline_tenant_ids
        await _delete_outbox_test_tenants(
            owner_sessionmaker,
            tracked_tenant_ids | new_tenant_ids,
        )

        # A task_ids=None recovery scan may legitimately project a terminal
        # task that belonged to a pre-existing tenant. Delete only IDs created
        # during this test; the foreign source task and all baseline facts stay.
        async with owner_sessionmaker() as db:
            new_outbox_ids = (
                set((await db.execute(select(RuntimeNotificationOutbox.id))).scalars().all()) - baseline_outbox_ids
            )
            if new_outbox_ids:
                await db.execute(
                    delete(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id.in_(new_outbox_ids))
                )
            new_task_ids = set((await db.execute(select(RuntimeTask.id))).scalars().all()) - baseline_task_ids
            if new_task_ids:
                await db.execute(delete(RuntimeTask).where(RuntimeTask.id.in_(new_task_ids)))
            await db.commit()

        async with owner_sessionmaker() as db:
            assert set((await db.execute(select(Tenant.id))).scalars().all()) - baseline_tenant_ids == set()
            assert set((await db.execute(select(RuntimeTask.id))).scalars().all()) - baseline_task_ids == set()
            assert (
                set((await db.execute(select(RuntimeNotificationOutbox.id))).scalars().all()) - baseline_outbox_ids
                == set()
            )
            assert set((await db.execute(select(Participant.id))).scalars().all()) - baseline_participant_ids == set()


@pytest.mark.usefixtures("migrated_pg_url")
async def test_outbox_cleanup_boundary_is_self_contained_and_tenant_scoped(owner_sessionmaker):
    tenant_a, user_a, agent_a, session_a = await _seed_external_scope_sentinel(owner_sessionmaker)
    task_a = uuid.uuid4()
    try:
        async with tenant_scoped_session(tenant_a, session_factory=owner_sessionmaker) as db:
            db.add(
                RuntimeTask(
                    id=task_a,
                    tenant_id=tenant_a,
                    task_type="workflow",
                    status="completed",
                    parent_agent_id=agent_a,
                    parent_session_id=str(session_a),
                    root_user_id=user_a,
                    created_at=datetime(1900, 1, 1, tzinfo=UTC),
                    result_summary="A remains closed across B cleanup",
                    metadata_json={"user_id": str(user_a)},
                )
            )
            await enqueue_completion_notification(
                db,
                CompletionNotification(
                    tenant_id=tenant_a,
                    source_kind="workflow",
                    source_run_id=str(task_a),
                    parent_session_id=session_a,
                    parent_agent_id=agent_a,
                    parent_user_id=user_a,
                    terminal_status="completed",
                    task_type="workflow",
                    summary="A already has its durable intent",
                ),
            )

        tenant_b, _user_b, agent_b, _session_b = await _seed_parent_session(owner_sessionmaker)
        async with owner_sessionmaker() as db:
            participant_b = await db.scalar(select(Agent.participant_id).where(Agent.id == agent_b))
        assert participant_b is not None

        await _delete_outbox_test_tenants(owner_sessionmaker, {tenant_b})

        async with owner_sessionmaker() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(RuntimeNotificationOutbox)
                    .where(
                        RuntimeNotificationOutbox.tenant_id == tenant_a,
                        RuntimeNotificationOutbox.source_run_id == str(task_a),
                    )
                )
                == 1
            )
            assert await db.get(Tenant, tenant_b) is None
            assert await db.get(Participant, participant_b) is None
    finally:
        await _delete_outbox_test_tenants(owner_sessionmaker, {tenant_a})


def test_reconcile_sql_correlates_plan_status_subqueries_only_to_outer_runtime_task() -> None:
    from app.services.runtime_notification_outbox import _build_reconcile_candidate_statement

    statement = _build_reconcile_candidate_statement(candidate_limit=20, task_ids=None)
    compiled = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    inner_froms = re.findall(
        r"EXISTS \(SELECT agent_plan_requests\.id\s+FROM ([^\n]+)",
        compiled,
    )
    assert len(inner_froms) >= 2
    assert all(value.strip() == "agent_plan_requests" for value in inner_froms)
    assert "FROM agent_plan_requests, runtime_tasks" not in compiled
    assert "FROM agent_plan_requests, runtime_notification_outbox" not in compiled


def _notification(*, tenant_id, user_id, agent_id, session_id, source_run_id="run-1", status="completed"):
    return CompletionNotification(
        tenant_id=tenant_id,
        source_kind="subagent",
        source_run_id=source_run_id,
        parent_session_id=session_id,
        parent_agent_id=agent_id,
        parent_user_id=user_id,
        terminal_status=status,
        task_type="subagent",
        summary="The worker completed.",
        child_agent_name="Researcher",
        delivery_mode="session_projection",
        metadata={"evidence_ref": "t0:event-1"},
    )


@pytest.mark.usefixtures("migrated_pg_url")
async def test_enqueue_is_deterministic_and_unique(owner_sessionmaker):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    notification = _notification(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        first = await enqueue_completion_notification(db, notification)
        second = await enqueue_completion_notification(db, notification)
        await db.commit()

    async with owner_sessionmaker() as db:
        count = (
            await db.execute(
                select(func.count())
                .select_from(RuntimeNotificationOutbox)
                .where(RuntimeNotificationOutbox.tenant_id == tenant_id)
            )
        ).scalar_one()

    assert first == second
    assert count == 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_authoritative_enqueue_upgrades_reconciled_payload_by_rank(owner_sessionmaker):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    base = dict(
        tenant_id=tenant_id,
        source_kind="a2a_delegation",
        source_run_id="ranked-run",
        parent_session_id=session_id,
        parent_agent_id=agent_id,
        parent_user_id=user_id,
        terminal_status="completed",
        task_type="delegation",
        delivery_mode="parent_continuation",
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        outbox_id = await enqueue_completion_notification(
            db,
            CompletionNotification(
                **base,
                summary="generic result",
                payload_rank=10,
                metadata={"reconciled_from_terminal_runtime_task": True},
            ),
        )
        await db.commit()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await enqueue_completion_notification(
            db,
            CompletionNotification(
                **base,
                summary="rich result",
                payload_rank=100,
                artifacts=[{"path": "workspace/report.md"}],
                metadata={"artifact_contract": "fulfilled"},
            ),
        )
        await db.commit()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await enqueue_completion_notification(
            db,
            CompletionNotification(
                **base,
                summary="late generic result",
                payload_rank=10,
            ),
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        stored = (
            await db.execute(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == outbox_id))
        ).scalar_one()
    assert stored.summary == "rich result"
    assert stored.payload_rank == 100
    assert stored.artifacts_json == [{"path": "workspace/report.md"}]
    assert stored.metadata_json == {"artifact_contract": "fulfilled"}


@pytest.mark.usefixtures("migrated_pg_url")
async def test_claim_retry_and_terminal_ack_are_durable(owner_sessionmaker):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    notification = _notification(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        outbox_id = await enqueue_completion_notification(db, notification)
        await db.commit()

    attempts: list[uuid.UUID] = []

    async def flaky_deliver(item):
        attempts.append(item.id)
        if len(attempts) == 1:
            raise RuntimeError("temporary delivery failure")
        return {"status": "started", "runtime_task_id": "parent-run"}

    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=3,
    )
    first = await service.drain_once(worker_id="worker-a", deliver=flaky_deliver, item_ids={outbox_id})
    second = await service.drain_once(worker_id="worker-b", deliver=flaky_deliver, item_ids={outbox_id})
    third = await service.drain_once(worker_id="worker-c", deliver=flaky_deliver, item_ids={outbox_id})

    async with owner_sessionmaker() as db:
        stored = (
            await db.execute(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == outbox_id))
        ).scalar_one()

    assert first == {"claimed": 1, "delivered": 0, "retried": 1, "deferred": 0, "dead_lettered": 0}
    assert second == {"claimed": 1, "delivered": 1, "retried": 0, "deferred": 0, "dead_lettered": 0}
    assert third == {"claimed": 0, "delivered": 0, "retried": 0, "deferred": 0, "dead_lettered": 0}
    assert attempts == [outbox_id, outbox_id]
    assert stored.status == "delivered"
    assert stored.attempt_count == 2
    assert stored.delivery_receipt_json["runtime_task_id"] == "parent-run"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_approval_continuation_retry_and_ack_update_approval_receipt(owner_sessionmaker):
    from app.models.audit import ApprovalRequest

    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    approval_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            ApprovalRequest(
                id=approval_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                action_type="workspace.write",
                status="approved",
                tool_name="write_file",
                execution_status="succeeded",
                execution_idempotency_key=f"approval:{approval_id}",
                execution_result="wrote report",
                execution_receipt={"status": "succeeded"},
                details={"session_id": str(session_id)},
            )
        )
        outbox_id = await enqueue_completion_notification(
            db,
            CompletionNotification(
                tenant_id=tenant_id,
                source_kind="approval",
                source_run_id=str(task_id),
                parent_session_id=session_id,
                parent_agent_id=agent_id,
                parent_user_id=user_id,
                terminal_status="completed",
                task_type="approval_execution",
                summary="wrote report",
                delivery_mode="parent_continuation",
                metadata={"approval_id": str(approval_id), "tool_name": "write_file"},
            ),
        )
        await db.commit()

    calls = 0

    async def flaky_delivery(_item):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary continuation failure")
        return {"status": "started", "runtime_task_id": "continued-run"}

    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=3,
    )
    assert (
        await service.drain_once(
            worker_id="approval-worker-a",
            deliver=flaky_delivery,
            item_ids={outbox_id},
        )
    )["retried"] == 1
    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        assert approval is not None
        assert approval.execution_receipt["continuation_status"] == "retrying"
        assert "temporary continuation failure" in approval.execution_receipt["continuation_error"]

    assert (
        await service.drain_once(
            worker_id="approval-worker-b",
            deliver=flaky_delivery,
            item_ids={outbox_id},
        )
    )["delivered"] == 1
    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        row = await db.get(RuntimeNotificationOutbox, outbox_id)
        assert approval is not None and row is not None
        assert row.status == "delivered"
        assert approval.execution_receipt["continuation_status"] == "delivered"
        assert approval.execution_receipt["continuation_attempt_count"] == 2
        assert "continuation_error" not in approval.execution_receipt


@pytest.mark.usefixtures("migrated_pg_url")
async def test_dead_lettered_team_close_reopens_team_for_retry(owner_sessionmaker):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    team_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        team = AgentTeam(
            id=team_id,
            tenant_id=tenant_id,
            lead_agent_id=agent_id,
            parent_session_id=session_id,
            name="Research Team",
            status="closing",
            metadata_json={"close_attempt": 1},
        )
        db.add(team)
        await db.flush()
        outbox_id = await enqueue_completion_notification(
            db,
            CompletionNotification(
                tenant_id=tenant_id,
                source_kind="agent_team",
                source_run_id=f"agent_team_close:{team_id}:1",
                parent_session_id=str(session_id),
                parent_agent_id=agent_id,
                parent_user_id=user_id,
                terminal_status="completed",
                task_type="agent_team_close",
                summary="Synthesize Team results.",
                delivery_mode="parent_continuation",
                metadata={"agent_team_close_id": str(team_id)},
            ),
        )
        team.metadata_json = {**team.metadata_json, "close_notification_id": str(outbox_id)}
        await db.commit()

    async def fail_delivery(_item):
        raise RuntimeError("parent continuation unavailable")

    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=1,
    )
    result = await service.drain_once(worker_id="worker-a", deliver=fail_delivery, item_ids={outbox_id})

    async with owner_sessionmaker() as db:
        team = (await db.execute(select(AgentTeam).where(AgentTeam.id == team_id))).scalar_one()
        events = list(
            (await db.execute(select(AgentTeamEvent).where(AgentTeamEvent.team_id == team_id))).scalars().all()
        )

    assert result["dead_lettered"] == 1
    assert team.status == "active"
    assert team.metadata_json["close_synthesis_status"] == "delivery_failed"
    assert "parent continuation unavailable" in team.metadata_json["close_failure"]
    assert any(event.event_type == "team_close_delivery_failed" for event in events)


@pytest.mark.usefixtures("migrated_pg_url")
async def test_team_close_waits_for_idle_parent_before_lead_synthesis(owner_sessionmaker, monkeypatch):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    notification = CompletionNotification(
        tenant_id=tenant_id,
        source_kind="agent_team",
        source_run_id="agent_team_close:team-1:1",
        parent_session_id=session_id,
        parent_agent_id=agent_id,
        parent_user_id=user_id,
        terminal_status="completed",
        task_type="agent_team_close",
        summary="Synthesize Team results.",
        delivery_mode="parent_continuation",
        metadata={"agent_team_close_id": str(uuid.uuid4())},
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        outbox_id = await enqueue_completion_notification(db, notification)
        await db.commit()

    continuation_calls = []

    async def active_run(**_kwargs):
        return {"run_id": str(uuid.uuid4()), "status": "running"}

    async def unexpected_continuation(**kwargs):
        continuation_calls.append(kwargs)
        return {"status": "started"}

    monkeypatch.setattr("app.services.web_chat_runtime.get_active_web_chat_run", active_run)
    monkeypatch.setattr(
        "app.services.agent_session_continuation.continue_parent_session_with_task_notification",
        unexpected_continuation,
    )
    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        deferred_retry_seconds=0,
    )

    result = await service.drain_once(worker_id="worker-a", item_ids={outbox_id})

    async with owner_sessionmaker() as db:
        row = (
            await db.execute(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == outbox_id))
        ).scalar_one()
    assert result["deferred"] == 1
    assert row.status == "pending"
    assert row.last_error == "parent_session_active"
    assert row.attempt_count == 0
    assert continuation_calls == []


@pytest.mark.usefixtures("migrated_pg_url")
async def test_expired_processing_lease_is_reclaimed(owner_sessionmaker):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    notification = _notification(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        outbox_id = await enqueue_completion_notification(db, notification)
        await db.commit()

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker, lease_seconds=5)
    now = datetime.now(UTC)
    first = await service.claim_batch(worker_id="crashed-worker", now=now, limit=1, item_ids={outbox_id})
    before_expiry = await service.claim_batch(
        worker_id="other-worker",
        now=now + timedelta(seconds=4),
        limit=1,
        item_ids={outbox_id},
    )
    reclaimed = await service.claim_batch(
        worker_id="other-worker",
        now=now + timedelta(seconds=6),
        limit=1,
        item_ids={outbox_id},
    )

    assert [item.id for item in first] == [outbox_id]
    assert before_expiry == []
    assert [item.id for item in reclaimed] == [outbox_id]
    assert reclaimed[0].attempt_count == 2


@pytest.mark.usefixtures("migrated_pg_url")
async def test_default_global_claim_orders_eligible_rows_without_touching_foreign_baseline(
    owner_sessionmaker,
):
    async with owner_sessionmaker() as db:
        foreign_before = {
            row.id: (row.status, row.locked_by, row.locked_at, row.attempt_count)
            for row in (await db.execute(select(RuntimeNotificationOutbox))).scalars().all()
        }

    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    first_id: uuid.UUID
    second_id: uuid.UUID
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        first_id = await enqueue_completion_notification(
            db,
            _notification(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                source_run_id="global-fairness-first",
            ),
        )
        second_id = await enqueue_completion_notification(
            db,
            _notification(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                source_run_id="global-fairness-second",
            ),
        )
        first = await db.get(RuntimeNotificationOutbox, first_id, with_for_update=True)
        second = await db.get(RuntimeNotificationOutbox, second_id, with_for_update=True)
        assert first is not None and second is not None
        first.available_at = datetime(1700, 1, 1, tzinfo=UTC)
        second.available_at = datetime(1700, 1, 2, tzinfo=UTC)

    claimed = await RuntimeNotificationOutboxService(session_factory=owner_sessionmaker).claim_batch(
        worker_id="global-fairness-worker",
        now=datetime(1800, 1, 1, tzinfo=UTC),
        limit=2,
    )
    assert [item.id for item in claimed] == [first_id, second_id]

    async with owner_sessionmaker() as db:
        foreign_after = {
            row.id: (row.status, row.locked_by, row.locked_at, row.attempt_count)
            for row in (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id.in_(foreign_before))
                )
            )
            .scalars()
            .all()
        }
    assert foreign_after == foreign_before


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.parametrize(
    "reclaim_worker_id",
    ("worker-b", "worker-a"),
    ids=("different-worker", "same-worker-new-epoch"),
)
async def test_stale_claim_cannot_continue_after_lease_reclaim(
    owner_sessionmaker,
    monkeypatch,
    reclaim_worker_id,
):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    notification = _notification(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        source_run_id=str(task_id),
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=user_id,
                result_summary="completed",
                metadata_json={},
            )
        )
        outbox_id = await enqueue_completion_notification(db, notification)
        await db.commit()

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker, lease_seconds=1)
    now = datetime.now(UTC)
    stale_item = (await service.claim_batch(worker_id="worker-a", now=now, limit=1, item_ids={outbox_id}))[0]
    current_item = (
        await service.claim_batch(
            worker_id=reclaim_worker_id,
            now=now + timedelta(seconds=2),
            limit=1,
            item_ids={outbox_id},
        )
    )[0]
    continuation_calls: list[dict] = []

    async def forbidden_late_continuation(**kwargs):
        continuation_calls.append(kwargs)
        return {"status": "started"}

    monkeypatch.setattr(
        "app.services.agent_session_continuation.continue_parent_session_with_task_notification",
        forbidden_late_continuation,
    )

    with pytest.raises(RuntimeError, match="claim"):
        await service._deliver(stale_item)

    async with owner_sessionmaker() as db:
        stored = (
            await db.execute(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == current_item.id))
        ).scalar_one()
    assert continuation_calls == []
    assert stored.status == "processing"
    assert stored.locked_by == reclaim_worker_id
    assert current_item.attempt_count == stale_item.attempt_count + 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_claim_snapshot_cannot_continue_after_manual_authority_transition(
    owner_sessionmaker,
    monkeypatch,
):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    notification = _notification(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        source_run_id=str(task_id),
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=user_id,
                result_summary="completed",
                metadata_json={},
            )
        )
        outbox_id = await enqueue_completion_notification(db, notification)
        await db.commit()

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    stale_item = (await service.claim_batch(worker_id="worker-a", limit=1, item_ids={outbox_id}))[0]
    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeNotificationOutbox, outbox_id, with_for_update=True)
        assert row is not None
        row.status = "pending"
        row.locked_by = None
        row.locked_at = None
        await db.commit()

    continuation_calls: list[dict] = []

    async def forbidden_stale_continuation(**kwargs):
        continuation_calls.append(kwargs)
        return {"status": "started"}

    monkeypatch.setattr(
        "app.services.agent_session_continuation.continue_parent_session_with_task_notification",
        forbidden_stale_continuation,
    )

    with pytest.raises(RuntimeError, match="claim"):
        await service._deliver(stale_item)

    assert continuation_calls == []


@pytest.mark.usefixtures("migrated_pg_url")
async def test_delivery_event_dedupes_when_ack_fails_after_commit(owner_sessionmaker, monkeypatch):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    notification = _notification(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        source_run_id=str(task_id),
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=user_id,
                result_summary="completed",
                metadata_json={},
            )
        )
        outbox_id = await enqueue_completion_notification(db, notification)
        await db.commit()

    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=3,
    )
    original_mark_delivered = service._mark_delivered
    failed_once = False

    async def fail_first_ack(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("worker crashed before ack")
        return await original_mark_delivered(*args, **kwargs)

    monkeypatch.setattr(service, "_mark_delivered", fail_first_ack)
    first = await service.drain_once(worker_id="worker-a", item_ids={outbox_id})
    second = await service.drain_once(worker_id="worker-b", item_ids={outbox_id})

    async with owner_sessionmaker() as db:
        stored = (
            await db.execute(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == outbox_id))
        ).scalar_one()
        events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.causation_id == outbox_id,
                        ChatTranscriptEvent.event_type == "agent_task_notification",
                    )
                )
            )
            .scalars()
            .all()
        )

    assert first["retried"] == 1
    assert second["delivered"] == 1
    assert stored.status == "delivered"
    assert stored.delivery_receipt_json["deduplicated"] is True
    assert len(events) == 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_outbox_rls_hides_other_tenant_rows(owner_sessionmaker, app_user_sessionmaker):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    other_tenant_id, *_ = await _seed_parent_session(owner_sessionmaker)
    notification = _notification(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await enqueue_completion_notification(db, notification)
        await db.commit()

    async with tenant_scoped_session(other_tenant_id, session_factory=app_user_sessionmaker) as db:
        rls_context = (
            await db.execute(
                text(
                    "SELECT current_user, current_setting('app.current_tenant_id', true), "
                    "row_security_active('runtime_notification_outbox'), "
                    "(SELECT relrowsecurity FROM pg_class WHERE oid = 'runtime_notification_outbox'::regclass), "
                    "(SELECT relforcerowsecurity FROM pg_class WHERE oid = 'runtime_notification_outbox'::regclass), "
                    "pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid = 'runtime_notification_outbox'::regclass))"
                )
            )
        ).one()
        visible = list((await db.execute(select(RuntimeNotificationOutbox))).scalars().all())

    assert rls_context[0] == "rls_app_user"
    assert rls_context[1] == str(other_tenant_id)
    assert rls_context[2:5] == (True, True, True)
    assert visible == [], rls_context


@pytest.mark.usefixtures("migrated_pg_url")
async def test_system_plan_reconciler_projects_resumable_failure_and_completion_with_retry(
    owner_sessionmaker,
):
    foreign_tenant, foreign_user, foreign_agent, foreign_session = await _seed_parent_session(owner_sessionmaker)
    foreign_task = uuid.uuid4()
    async with tenant_scoped_session(foreign_tenant, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=foreign_task,
                tenant_id=foreign_tenant,
                task_type="workflow",
                status="completed",
                parent_agent_id=foreign_agent,
                parent_session_id=str(foreign_session),
                root_user_id=foreign_user,
                result_summary="foreign closed delivery sentinel",
                metadata_json={"user_id": str(foreign_user)},
            )
        )
        foreign_outbox_id = await enqueue_completion_notification(
            db,
            CompletionNotification(
                tenant_id=foreign_tenant,
                source_kind="workflow",
                source_run_id=str(foreign_task),
                parent_session_id=foreign_session,
                parent_agent_id=foreign_agent,
                parent_user_id=foreign_user,
                terminal_status="completed",
                task_type="workflow",
                summary="foreign closed delivery sentinel",
            ),
        )
        foreign_row = await db.get(RuntimeNotificationOutbox, foreign_outbox_id, with_for_update=True)
        assert foreign_row is not None
        foreign_row.status = "delivered"

    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(
        owner_sessionmaker,
        source_channel="wechat_personal",
    )
    plan_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            AgentPlanRequest(
                id=plan_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=str(session_id),
                requested_by_user_id=user_id,
                source="wechat_personal",
                intent_type="in_session_execution",
                original_request="先生成一个可确认计划",
                status="draft",
                plan_json={},
                metadata_json={},
            )
        )
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="system_plan_run",
                status="resumable",
                parent_agent_id=agent_id,
                child_agent_id=agent_id,
                parent_session_id=str(session_id),
                child_session_id=str(session_id),
                root_user_id=user_id,
                root_session_id=str(session_id),
                result_summary="provider disconnected; automatic retry scheduled",
                metadata_json={
                    "source": "system_plan_run",
                    "plan_id": str(plan_id),
                    "input_revision": 1,
                    "resumable_system_plan": True,
                },
            )
        )

    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
    )
    assert await service.reconcile_terminal_tasks_once(limit=10, task_ids={task_id}) == 1
    async with owner_sessionmaker() as db:
        target = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.source_kind == "system_plan_run",
                    RuntimeNotificationOutbox.source_run_id == str(task_id),
                    RuntimeNotificationOutbox.terminal_status == "resumable",
                )
            )
        ).scalar_one()
        assert target.metadata_json["plan_id"] == str(plan_id)
        assert target.delivery_mode == "session_projection"

    async with owner_sessionmaker() as db:
        assert await db.get(RuntimeNotificationOutbox, foreign_outbox_id) is not None

    deliveries = 0

    # Test Double rationale: only the external continuation delivery is failed;
    # claim, retry lease, and durable acknowledgment use the real outbox table.
    async def disconnect_once(item):
        nonlocal deliveries
        deliveries += 1
        assert item.source_kind == "system_plan_run"
        assert item.terminal_status == "resumable"
        if deliveries == 1:
            raise RuntimeError("channel disconnected")
        return {"status": "projected-after-retry"}

    first = await service.drain_once(
        worker_id="system-plan-outbox",
        deliver=disconnect_once,
        limit=10,
        item_ids={target.id},
    )
    second = await service.drain_once(
        worker_id="system-plan-outbox",
        deliver=disconnect_once,
        limit=10,
        item_ids={target.id},
    )
    assert first["retried"] == 1
    assert second["delivered"] == 1

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        assert task is not None
        task.status = "completed"
        task.completed_at = datetime.now(UTC)
        task.result_summary = "Reconciliation resolved: no confirmable plan was authored."
        task.metadata_json = {
            **(task.metadata_json or {}),
            "reconciliation_operation": {
                "action": "mark_resolved",
                "status": "completed",
            },
        }

    assert await service.reconcile_terminal_tasks_once(limit=10, task_ids={task_id}) == 1
    async with owner_sessionmaker() as db:
        failed = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.source_run_id == str(task_id),
                    RuntimeNotificationOutbox.terminal_status == "failed",
                )
            )
        ).scalar_one()
        assert failed.source_kind == "system_plan_run"
        assert failed.metadata_json["plan_status"] == "draft"
        assert "regenerate" in failed.summary.lower()

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        plan = await db.get(AgentPlanRequest, plan_id, with_for_update=True)
        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        assert plan is not None and task is not None
        plan.status = "awaiting_confirmation"
        plan.plan_hash = "sha256:ready"
        plan.plan_json = {"schema": "hive_plan.v1", "title": "Ready"}
        task.result_summary = "Plan authoring completed."

    assert await service.reconcile_terminal_tasks_once(limit=10, task_ids={task_id}) == 1
    async with owner_sessionmaker() as db:
        rows = list(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(task_id))
                )
            ).scalars()
        )
    assert {row.terminal_status for row in rows} == {"resumable", "failed", "completed"}
    assert all(row.source_kind == "system_plan_run" for row in rows)
    assert all(row.delivery_mode == "session_projection" for row in rows)


@pytest.mark.parametrize(
    ("terminal_status", "runtime_event_type"),
    [
        ("completed", "runtime_action_completed"),
        ("resumable", "runtime_action_progress"),
        ("needs_reconciliation", "runtime_action_blocked"),
    ],
)
@pytest.mark.usefixtures("migrated_pg_url")
async def test_system_plan_session_projection_writes_transcript_without_queueing_or_starting_run(
    owner_sessionmaker,
    monkeypatch,
    terminal_status: str,
    runtime_event_type: str,
):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    source_run_id = str(uuid.uuid4())
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=uuid.UUID(source_run_id),
                tenant_id=tenant_id,
                task_type="system_plan_run",
                status=terminal_status,
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=user_id,
                result_summary=f"System Plan {terminal_status}",
                metadata_json={},
            )
        )
        outbox_id = await enqueue_completion_notification(
            db,
            CompletionNotification(
                tenant_id=tenant_id,
                source_kind="system_plan_run",
                source_run_id=source_run_id,
                parent_session_id=session_id,
                parent_agent_id=agent_id,
                parent_user_id=user_id,
                terminal_status=terminal_status,
                task_type="system_plan_run",
                summary=f"System Plan {terminal_status}",
                delivery_mode="session_projection",
                metadata={"plan_id": str(uuid.uuid4())},
            ),
        )

    async def forbidden_runtime_wake(*_args, **_kwargs):
        raise AssertionError("System Plan session projection must not queue or start a web-chat run")

    monkeypatch.setattr("app.services.agent_session_continuation._find_active_run", forbidden_runtime_wake)
    monkeypatch.setattr(
        "app.services.agent_session_continuation._queue_saved_mid_run_user_message",
        forbidden_runtime_wake,
    )
    monkeypatch.setattr("app.services.agent_session_continuation.start_web_chat_run", forbidden_runtime_wake)

    result = await RuntimeNotificationOutboxService(session_factory=owner_sessionmaker).drain_once(
        worker_id=f"system-plan-{terminal_status}",
        limit=1,
        item_ids={outbox_id},
    )
    assert result["delivered"] == 1
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.causation_id == outbox_id,
                    )
                )
            ).scalars()
        )
        runtime_runs = await db.scalar(
            select(func.count())
            .select_from(RuntimeTask)
            .where(
                RuntimeTask.tenant_id == tenant_id,
                RuntimeTask.task_type == "web_chat_turn",
            )
        )
    assert {event.event_type for event in events} == {runtime_event_type, "agent_task_notification"}
    assert runtime_runs == 0


@pytest.mark.usefixtures("migrated_pg_url")
async def test_system_plan_reconcile_maps_all_statuses_with_cross_tenant_opposites(
    owner_sessionmaker,
):
    tenant_a = await _seed_parent_session(owner_sessionmaker)
    tenant_b = await _seed_parent_session(owner_sessionmaker)
    expected: dict[str, tuple[uuid.UUID, str]] = {}

    async def seed_cases(authority, cases):
        tenant_id, user_id, agent_id, session_id = authority
        async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
            for task_status, plan_status, expected_status in cases:
                plan_id = uuid.uuid4()
                task_id = uuid.uuid4()
                db.add(
                    AgentPlanRequest(
                        id=plan_id,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        session_id=str(session_id),
                        requested_by_user_id=user_id,
                        source="web",
                        intent_type="in_session_execution",
                        original_request=f"{task_status}/{plan_status}",
                        status=plan_status,
                        plan_json={},
                        metadata_json={},
                    )
                )
                db.add(
                    RuntimeTask(
                        id=task_id,
                        tenant_id=tenant_id,
                        task_type="system_plan_run",
                        status=task_status,
                        parent_agent_id=agent_id,
                        child_agent_id=agent_id,
                        parent_session_id=str(session_id),
                        root_user_id=user_id,
                        result_summary=f"{task_status}/{plan_status}",
                        metadata_json={"plan_id": str(plan_id)},
                    )
                )
                expected[str(task_id)] = (tenant_id, expected_status)

    await seed_cases(
        tenant_a,
        [
            ("resumable", "rejected", "resumable"),
            ("needs_reconciliation", "awaiting_confirmation", "needs_reconciliation"),
            ("completed", "awaiting_confirmation", "completed"),
        ],
    )
    await seed_cases(
        tenant_b,
        [
            ("completed", "rejected", "skipped"),
            ("killed", "draft", "killed"),
            ("completed", "draft", "failed"),
        ],
    )

    repaired = await RuntimeNotificationOutboxService(session_factory=owner_sessionmaker).reconcile_terminal_tasks_once(
        limit=10,
        task_ids={uuid.UUID(task_id) for task_id in expected},
    )
    assert repaired == 6
    async with owner_sessionmaker() as db:
        rows = list(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(
                        RuntimeNotificationOutbox.source_kind == "system_plan_run",
                        RuntimeNotificationOutbox.source_run_id.in_(expected),
                    )
                )
            ).scalars()
        )
    assert len(rows) == 6
    assert {row.source_run_id: (row.tenant_id, row.terminal_status) for row in rows} == expected
    assert all(row.delivery_mode == "session_projection" for row in rows)


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reconciler_db_filters_delivered_prefix_and_stays_one_batch_when_fully_satisfied(
    owner_sessionmaker,
):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    missing_task_id = uuid.uuid4()
    scoped_task_ids = {missing_task_id}
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=missing_task_id,
                tenant_id=tenant_id,
                task_type="workflow",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                result_summary="missing durable completion intent",
                metadata_json={"user_id": str(user_id)},
            )
        )
        for index in range(25):
            delivered_task_id = uuid.uuid4()
            scoped_task_ids.add(delivered_task_id)
            db.add(
                RuntimeTask(
                    id=delivered_task_id,
                    tenant_id=tenant_id,
                    task_type="workflow",
                    status="completed",
                    parent_agent_id=agent_id,
                    parent_session_id=str(session_id),
                    result_summary=f"already delivered {index}",
                    metadata_json={"user_id": str(user_id)},
                )
            )
            await enqueue_completion_notification(
                db,
                CompletionNotification(
                    tenant_id=tenant_id,
                    source_kind="workflow",
                    source_run_id=str(delivered_task_id),
                    parent_session_id=session_id,
                    parent_agent_id=agent_id,
                    parent_user_id=user_id,
                    terminal_status="completed",
                    task_type="workflow",
                    summary=f"already delivered {index}",
                ),
            )

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    engine = owner_sessionmaker.kw["bind"].sync_engine
    runtime_task_selects = 0

    def count_runtime_task_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
        nonlocal runtime_task_selects
        normalized = statement.lower()
        if normalized.lstrip().startswith("select") and "from runtime_tasks" in normalized:
            runtime_task_selects += 1

    event.listen(engine, "before_cursor_execute", count_runtime_task_selects)
    try:
        assert await service.reconcile_terminal_tasks_once(limit=1, task_ids=scoped_task_ids) == 1
        assert runtime_task_selects == 1
        async with owner_sessionmaker() as db:
            repaired = (
                await db.execute(
                    select(RuntimeNotificationOutbox.id).where(
                        RuntimeNotificationOutbox.source_run_id == str(missing_task_id)
                    )
                )
            ).scalar_one()
            assert repaired is not None

        runtime_task_selects = 0
        assert await service.reconcile_terminal_tasks_once(limit=1, task_ids=scoped_task_ids) == 0
        assert runtime_task_selects == 1
    finally:
        event.remove(engine, "before_cursor_execute", count_runtime_task_selects)


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reconciler_filters_newer_invalid_targets_before_limit_so_older_valid_task_is_repaired(
    owner_sessionmaker,
):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    plan_id = uuid.uuid4()
    valid_task_id = uuid.uuid4()
    candidate_ids = {valid_task_id}
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            AgentPlanRequest(
                id=plan_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=str(session_id),
                requested_by_user_id=user_id,
                source="web",
                intent_type="in_session_execution",
                original_request="valid older plan",
                status="draft",
                plan_json={},
                metadata_json={},
            )
        )
        db.add(
            RuntimeTask(
                id=valid_task_id,
                tenant_id=tenant_id,
                task_type="system_plan_run",
                status="resumable",
                parent_agent_id=agent_id,
                child_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=user_id,
                result_summary="valid older recovery",
                metadata_json={"plan_id": str(plan_id)},
            )
        )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        for index in range(20):
            invalid_task_id = uuid.uuid4()
            candidate_ids.add(invalid_task_id)
            db.add(
                RuntimeTask(
                    id=invalid_task_id,
                    tenant_id=tenant_id,
                    task_type="system_plan_run",
                    status="resumable",
                    parent_agent_id=agent_id,
                    child_agent_id=agent_id,
                    parent_session_id=(f"plan-invalid-{index}" if index % 2 == 0 else str(uuid.uuid4())),
                    root_user_id=(None if index % 3 == 0 else user_id),
                    result_summary="invalid newer recovery target",
                    metadata_json={"plan_id": str(plan_id)},
                )
            )

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    assert await service.reconcile_terminal_tasks_once(limit=1, task_ids=candidate_ids) == 1
    async with owner_sessionmaker() as db:
        repaired = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(valid_task_id))
            )
        ).scalar_one()
    assert repaired.delivery_mode == "session_projection"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reconciler_excludes_old_dead_letter_prefix_before_bounded_limit(
    owner_sessionmaker,
):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    candidate_ids: set[uuid.UUID] = set()
    old_created_at = datetime(1900, 1, 1, tzinfo=UTC)
    valid_task_id = uuid.uuid4()

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        for index in range(20):
            task_id = uuid.uuid4()
            candidate_ids.add(task_id)
            db.add(
                RuntimeTask(
                    id=task_id,
                    tenant_id=tenant_id,
                    task_type="workflow",
                    status="completed",
                    parent_agent_id=agent_id,
                    parent_session_id=str(session_id),
                    created_at=old_created_at + timedelta(seconds=index),
                    result_summary=f"old dead-letter delivery {index}",
                    metadata_json={"user_id": str(user_id)},
                )
            )
            outbox_id = await enqueue_completion_notification(
                db,
                CompletionNotification(
                    tenant_id=tenant_id,
                    source_kind="workflow",
                    source_run_id=str(task_id),
                    parent_session_id=session_id,
                    parent_agent_id=agent_id,
                    parent_user_id=user_id,
                    terminal_status="completed",
                    task_type="workflow",
                    summary=f"old dead-letter delivery {index}",
                ),
            )
            outbox = await db.get(RuntimeNotificationOutbox, outbox_id, with_for_update=True)
            assert outbox is not None
            outbox.status = "dead_letter"
            outbox.last_error = "operator action required"

        candidate_ids.add(valid_task_id)
        db.add(
            RuntimeTask(
                id=valid_task_id,
                tenant_id=tenant_id,
                task_type="workflow",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                created_at=datetime(2100, 1, 1, tzinfo=UTC),
                result_summary="newer valid missing delivery intent",
                metadata_json={"user_id": str(user_id)},
            )
        )

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    assert await service.reconcile_terminal_tasks_once(limit=1, task_ids=candidate_ids) == 1

    async with owner_sessionmaker() as db:
        repaired = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.source_run_id == str(valid_task_id),
                )
            )
        ).scalar_one()
        dead_letters = list(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(
                        RuntimeNotificationOutbox.source_run_id.in_(
                            {str(task_id) for task_id in candidate_ids - {valid_task_id}}
                        )
                    )
                )
            ).scalars()
        )
    assert repaired.status == "pending"
    assert len(dead_letters) == 20
    assert all(row.status == "dead_letter" for row in dead_letters)


@pytest.mark.parametrize(
    ("old_task_type", "old_status", "new_task_type", "new_status"),
    [
        ("system_plan_run", "resumable", "workflow", "completed"),
        ("workflow", "completed", "system_plan_run", "resumable"),
    ],
)
@pytest.mark.usefixtures("migrated_pg_url")
async def test_reconciler_advances_oldest_candidate_under_continuous_cross_lane_inflow(
    owner_sessionmaker,
    old_task_type,
    old_status,
    new_task_type,
    new_status,
):
    """Both recovery and ordinary terminal candidates must make bounded progress."""

    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    plan_id = uuid.uuid4()
    old_task_id = uuid.uuid4()
    old_created_at = datetime(1900, 1, 1, tzinfo=UTC)
    inflow_start = datetime(2200, 1, 1, tzinfo=UTC)

    def candidate(task_id, *, task_type, status, created_at, summary):
        is_system_plan = task_type == "system_plan_run"
        return RuntimeTask(
            id=task_id,
            tenant_id=tenant_id,
            task_type=task_type,
            status=status,
            parent_agent_id=agent_id,
            child_agent_id=agent_id if is_system_plan else None,
            parent_session_id=str(session_id),
            root_user_id=user_id if is_system_plan else None,
            created_at=created_at,
            result_summary=summary,
            metadata_json=(
                {"plan_id": str(plan_id)}
                if is_system_plan
                else {"user_id": str(user_id), "parent_session_id": str(session_id)}
            ),
        )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            AgentPlanRequest(
                id=plan_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=str(session_id),
                requested_by_user_id=user_id,
                source="web",
                intent_type="in_session_execution",
                original_request="fair reconciliation ordering",
                status="draft",
                plan_json={},
                metadata_json={},
            )
        )
        db.add(
            candidate(
                old_task_id,
                task_type=old_task_type,
                status=old_status,
                created_at=old_created_at,
                summary="oldest candidate must not starve",
            )
        )

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    assert await service.reconcile_terminal_tasks_once(limit=1, task_ids=set()) == 0

    # Each sweep receives a full newer candidate batch. Newest-first ordering
    # therefore starves the old candidate forever; fair oldest-first ordering
    # repairs it in the first bounded sweep regardless of candidate class.
    for tick in range(3):
        async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
            for index in range(20):
                db.add(
                    candidate(
                        uuid.uuid4(),
                        task_type=new_task_type,
                        status=new_status,
                        created_at=inflow_start + timedelta(minutes=tick, seconds=index),
                        summary=f"newer inflow tick={tick} index={index}",
                    )
                )
        assert await service.reconcile_terminal_tasks_once(limit=1, task_ids=None) == 1

    async with owner_sessionmaker() as db:
        repaired_old = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.source_run_id == str(old_task_id),
                )
            )
        ).scalar_one_or_none()
    assert repaired_old is not None
    expected_delivery_mode = "session_projection" if old_task_type == "system_plan_run" else "parent_continuation"
    assert repaired_old.delivery_mode == expected_delivery_mode


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reconciler_prefers_recovery_then_uuid_for_equal_age_candidates(owner_sessionmaker):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    plan_id = uuid.uuid4()
    ordinary_id = uuid.UUID(int=1)
    first_recovery_id = uuid.UUID(int=2)
    second_recovery_id = uuid.UUID(int=3)
    created_at = datetime(2000, 1, 1, tzinfo=UTC)
    candidate_ids = {ordinary_id, first_recovery_id, second_recovery_id}

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            AgentPlanRequest(
                id=plan_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=str(session_id),
                requested_by_user_id=user_id,
                source="web",
                intent_type="in_session_execution",
                original_request="stable equal-age reconciliation ordering",
                status="draft",
                plan_json={},
                metadata_json={},
            )
        )
        db.add(
            RuntimeTask(
                id=ordinary_id,
                tenant_id=tenant_id,
                task_type="workflow",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                created_at=created_at,
                result_summary="equal-age ordinary terminal",
                metadata_json={"user_id": str(user_id), "parent_session_id": str(session_id)},
            )
        )
        for task_id in (second_recovery_id, first_recovery_id):
            db.add(
                RuntimeTask(
                    id=task_id,
                    tenant_id=tenant_id,
                    task_type="system_plan_run",
                    status="resumable",
                    parent_agent_id=agent_id,
                    child_agent_id=agent_id,
                    parent_session_id=str(session_id),
                    root_user_id=user_id,
                    created_at=created_at,
                    result_summary="equal-age recovery candidate",
                    metadata_json={"plan_id": str(plan_id)},
                )
            )

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    for expected_task_id in (first_recovery_id, second_recovery_id, ordinary_id):
        assert await service.reconcile_terminal_tasks_once(limit=1, task_ids=candidate_ids) == 1
        async with owner_sessionmaker() as db:
            delivered_ids = set(
                (
                    await db.execute(
                        select(RuntimeNotificationOutbox.source_run_id).where(
                            RuntimeNotificationOutbox.source_run_id.in_({str(value) for value in candidate_ids})
                        )
                    )
                ).scalars()
            )
        assert str(expected_task_id) in delivered_ids


@pytest.mark.usefixtures("migrated_pg_url")
async def test_system_plan_resumable_dead_letter_operator_retry_is_delivery_only(
    owner_sessionmaker,
    monkeypatch,
):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    plan_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            AgentPlanRequest(
                id=plan_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=str(session_id),
                requested_by_user_id=user_id,
                source="web",
                intent_type="in_session_execution",
                original_request="author a resumable plan",
                status="draft",
                plan_json={},
                metadata_json={},
            )
        )
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="system_plan_run",
                status="resumable",
                parent_agent_id=agent_id,
                child_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=user_id,
                result_summary="provider disconnected; retry scheduled",
                metadata_json={"plan_id": str(plan_id)},
            )
        )
        outbox_id = await enqueue_completion_notification(
            db,
            CompletionNotification(
                tenant_id=tenant_id,
                source_kind="system_plan_run",
                source_run_id=str(task_id),
                parent_session_id=session_id,
                parent_agent_id=agent_id,
                parent_user_id=user_id,
                terminal_status="resumable",
                task_type="system_plan_run",
                summary="provider disconnected; retry scheduled",
                delivery_mode="session_projection",
                metadata={"plan_id": str(plan_id)},
            ),
        )

    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        max_attempts=1,
        retry_base_seconds=0,
    )

    async def channel_failure(_item):
        raise RuntimeError("channel unavailable")

    failed = await service.drain_once(
        worker_id="system-plan-dead-letter",
        deliver=channel_failure,
        limit=1,
        item_ids={outbox_id},
    )
    assert failed["dead_lettered"] == 1

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        [listed] = await list_runtime_notification_delivery_reconciliations(
            db,
            tenant_id=tenant_id,
            status="dead_letter",
            limit=10,
        )
        assert listed["delivery_id"] == str(outbox_id)
        assert listed["execution_terminal_status"] == "resumable"
        assert listed["retryable"] is True
        retried = await retry_runtime_notification_delivery(
            db,
            tenant_id=tenant_id,
            delivery_id=outbox_id,
            reason="operator verified the session projection target",
            actor_user_id=user_id,
        )
        assert retried["status"] == "pending"
        assert retried["retryable"] is False

    async def forbidden_runtime_or_model_call(*_args, **_kwargs):
        raise AssertionError("delivery-only System Plan retry must not start a model or web-chat run")

    monkeypatch.setattr("app.services.agent_session_continuation._find_active_run", forbidden_runtime_or_model_call)
    monkeypatch.setattr(
        "app.services.agent_session_continuation._queue_saved_mid_run_user_message",
        forbidden_runtime_or_model_call,
    )
    monkeypatch.setattr("app.services.agent_session_continuation.start_web_chat_run", forbidden_runtime_or_model_call)
    monkeypatch.setattr("app.runtime.invoker.invoke_agent", forbidden_runtime_or_model_call)

    delivered = await service.drain_once(
        worker_id="system-plan-delivery-retry",
        limit=1,
        item_ids={outbox_id},
    )
    assert delivered["delivered"] == 1

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, task_id)
        assert task is not None
        assert task.status == "resumable"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RuntimeTask)
                .where(
                    RuntimeTask.tenant_id == tenant_id,
                    RuntimeTask.task_type == "web_chat_turn",
                )
            )
            == 0
        )
        projected = await db.scalar(
            select(func.count())
            .select_from(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.session_id == session_id,
                ChatTranscriptEvent.causation_id == outbox_id,
                ChatTranscriptEvent.event_type == "agent_task_notification",
            )
        )
        assert projected == 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_terminal_repair_uses_chat_session_owner_and_repairs_stable_wrong_owner_intent(
    owner_sessionmaker,
):
    tenant_id, canonical_user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    recorded_user_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            User(
                id=recorded_user_id,
                username=f"recorded-{recorded_user_id.hex[:10]}",
                email=f"{recorded_user_id.hex[:12]}@outbox.test",
                password_hash="x",
                display_name="Stale Recorded Owner",
                tenant_id=tenant_id,
            )
        )
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="workflow",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=canonical_user_id,
                result_summary="canonical owner repair",
                metadata_json={
                    "user_id": str(recorded_user_id),
                    "owner_id": str(recorded_user_id),
                    "parent_session_id": str(session_id),
                },
            )
        )
        outbox_id = await enqueue_completion_notification(
            db,
            CompletionNotification(
                tenant_id=tenant_id,
                source_kind="workflow",
                source_run_id=str(task_id),
                parent_session_id=session_id,
                parent_agent_id=agent_id,
                parent_user_id=recorded_user_id,
                terminal_status="completed",
                task_type="workflow",
                summary="stale stable intent",
                payload_rank=100,
            ),
        )
        stale = await db.get(RuntimeNotificationOutbox, outbox_id, with_for_update=True)
        assert stale is not None
        stale.status = "dead_letter"
        stale.last_error = "completion target authority no longer resolves"
        stale.metadata_json = {
            "delivery_reconciliation": {
                "failure_kind": "authority_invalid",
                "authority_snapshot": {"valid": False},
                "automatic_retry_count": 0,
            }
        }

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    assert await service.reconcile_terminal_tasks_once(limit=1, task_ids={task_id}) == 1

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        repaired = await db.get(RuntimeNotificationOutbox, outbox_id)
        assert repaired is not None
        assert repaired.status == "pending"
        assert repaired.parent_user_id == canonical_user_id
        assert "user_id" not in repaired.metadata_json
        assert "owner_id" not in repaired.metadata_json
        assert repaired.metadata_json["recorded_parent_user_mismatch"] == {
            "metadata_user_id": str(recorded_user_id),
            "canonical_session_user_id": str(canonical_user_id),
        }


@pytest.mark.usefixtures("migrated_pg_url")
async def test_automatic_dead_letter_repair_filters_static_ineligible_prefix_before_limit(
    owner_sessionmaker,
):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    eligible_outbox_id: uuid.UUID | None = None
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        for index in range(101):
            task_id = uuid.uuid4()
            db.add(
                RuntimeTask(
                    id=task_id,
                    tenant_id=tenant_id,
                    task_type="subagent",
                    status="completed",
                    parent_agent_id=agent_id,
                    parent_session_id=str(session_id),
                    root_user_id=user_id,
                    result_summary=f"static eligibility {index}",
                    metadata_json={},
                )
            )
            outbox_id = await enqueue_completion_notification(
                db,
                _notification(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    source_run_id=str(task_id),
                ),
            )
            row = await db.get(RuntimeNotificationOutbox, outbox_id, with_for_update=True)
            assert row is not None
            row.status = "dead_letter"
            row.available_at = datetime(1900, 1, 1, tzinfo=UTC) + timedelta(seconds=index)
            row.metadata_json = {
                "delivery_reconciliation": {
                    "failure_kind": "authority_invalid" if index == 100 else "delivery_failure",
                    "authority_snapshot": {"valid": False},
                    "automatic_retry_count": 0,
                }
            }
            if index == 100:
                eligible_outbox_id = outbox_id
    assert eligible_outbox_id is not None

    assert await service.retry_recoverable_dead_letters_once(limit=1) == 1
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        repaired = await db.get(RuntimeNotificationOutbox, eligible_outbox_id)
        assert repaired is not None
        assert repaired.status == "pending"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_automatic_dead_letter_repair_rotates_dynamic_poison_and_reaches_repaired_101st(
    owner_sessionmaker,
):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    wrong_user_id = uuid.uuid4()
    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker, deferred_retry_seconds=60)
    eligible_outbox_id: uuid.UUID | None = None
    poison_ids: list[uuid.UUID] = []
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            User(
                id=wrong_user_id,
                username=f"poison-{wrong_user_id.hex[:10]}",
                email=f"{wrong_user_id.hex[:12]}@outbox.test",
                password_hash="x",
                display_name="Wrong Target Owner",
                tenant_id=tenant_id,
            )
        )
        for index in range(101):
            task_id = uuid.uuid4()
            db.add(
                RuntimeTask(
                    id=task_id,
                    tenant_id=tenant_id,
                    task_type="subagent",
                    status="completed",
                    parent_agent_id=agent_id,
                    parent_session_id=str(session_id),
                    root_user_id=user_id,
                    result_summary=f"dynamic authority {index}",
                    metadata_json={},
                )
            )
            outbox_id = await enqueue_completion_notification(
                db,
                _notification(
                    tenant_id=tenant_id,
                    user_id=(user_id if index == 100 else wrong_user_id),
                    agent_id=agent_id,
                    session_id=session_id,
                    source_run_id=str(task_id),
                ),
            )
            row = await db.get(RuntimeNotificationOutbox, outbox_id, with_for_update=True)
            assert row is not None
            row.status = "dead_letter"
            row.available_at = datetime(1900, 1, 1, tzinfo=UTC) + timedelta(seconds=index)
            row.metadata_json = {
                "delivery_reconciliation": {
                    "failure_kind": "authority_invalid",
                    "authority_snapshot": {"valid": False},
                    "automatic_retry_count": 0,
                }
            }
            if index == 100:
                eligible_outbox_id = outbox_id
            else:
                poison_ids.append(outbox_id)
    assert eligible_outbox_id is not None

    before = datetime.now(UTC)
    assert await service.retry_recoverable_dead_letters_once(limit=1) == 1
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        repaired = await db.get(RuntimeNotificationOutbox, eligible_outbox_id)
        poison = await db.get(RuntimeNotificationOutbox, poison_ids[0])
        assert repaired is not None and repaired.status == "pending"
        assert poison is not None and poison.status == "dead_letter"
        assert poison.available_at > before


@pytest.mark.usefixtures("migrated_pg_url")
async def test_manual_generic_retry_rejects_invalid_authority_and_delivery_race_with_inactive_user(
    owner_sessionmaker,
):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=user_id,
                result_summary="completed once",
                metadata_json={},
            )
        )
        outbox_id = await enqueue_completion_notification(
            db,
            _notification(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                source_run_id=str(task_id),
            ),
        )
        row = await db.get(RuntimeNotificationOutbox, outbox_id, with_for_update=True)
        assert row is not None
        row.status = "dead_letter"
        user = await db.get(User, user_id, with_for_update=True)
        assert user is not None
        user.is_active = False

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        [listed] = await list_runtime_notification_delivery_reconciliations(
            db,
            tenant_id=tenant_id,
            status="dead_letter",
            limit=10,
        )
        assert listed["parent_agent_id"] == str(agent_id)
        assert listed["parent_user_id"] == str(user_id)
        assert listed["parent_session_id"] == str(session_id)
        assert listed["authority_snapshot"]["valid"] is False
        assert listed["retryable"] is False
        with pytest.raises(ValueError, match="target authority"):
            await retry_runtime_notification_delivery(
                db,
                tenant_id=tenant_id,
                delivery_id=outbox_id,
                reason="operator verified repaired authority",
                actor_user_id=user_id,
            )
        await db.rollback()

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        user = await db.get(User, user_id, with_for_update=True)
        assert user is not None
        user.is_active = True
        await retry_runtime_notification_delivery(
            db,
            tenant_id=tenant_id,
            delivery_id=outbox_id,
            reason="  operator verified repaired authority  ",
            actor_user_id=user_id,
        )
        user = await db.get(User, user_id, with_for_update=True)
        assert user is not None
        user.is_active = False

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker, max_attempts=1)
    result = await service.drain_once(
        worker_id="inactive-authority-race",
        limit=1,
        item_ids={outbox_id},
    )
    assert result["dead_lettered"] == 1
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        row = await db.get(RuntimeNotificationOutbox, outbox_id)
        assert row is not None and row.status == "dead_letter"
        assert "authority no longer resolves" in str(row.last_error)
        task = await db.get(RuntimeTask, task_id)
        assert task is not None and task.status == "completed"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_a2a_dead_letter_accepts_target_owned_child_session_authority(owner_sessionmaker):
    tenant_id, user_id, parent_agent_id, parent_session_id = await _seed_parent_session(owner_sessionmaker)
    target_agent_id = uuid.uuid4()
    child_session_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            Agent(
                id=target_agent_id,
                tenant_id=tenant_id,
                name="A2A Target Agent",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await db.flush()
        db.add(
            ChatSession(
                id=child_session_id,
                tenant_id=tenant_id,
                agent_id=target_agent_id,
                user_id=user_id,
                title="A2A Child",
                source_channel="agent",
                session_kind="delegation_run",
                parent_session_id=parent_session_id,
                root_session_id=parent_session_id,
            )
        )
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="delegation",
                status="completed",
                parent_agent_id=parent_agent_id,
                child_agent_id=target_agent_id,
                parent_session_id=str(parent_session_id),
                child_session_id=str(child_session_id),
                root_user_id=user_id,
                result_summary="target completed",
                metadata_json={},
            )
        )
        outbox_id = await enqueue_completion_notification(
            db,
            CompletionNotification(
                tenant_id=tenant_id,
                source_kind="a2a_delegation",
                source_run_id=str(task_id),
                parent_session_id=parent_session_id,
                parent_agent_id=parent_agent_id,
                parent_user_id=user_id,
                child_session_id=child_session_id,
                child_agent_name="A2A Target Agent",
                terminal_status="completed",
                task_type="delegation",
                summary="target completed",
            ),
        )
        row = await db.get(RuntimeNotificationOutbox, outbox_id, with_for_update=True)
        assert row is not None
        row.status = "dead_letter"

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        [listed] = await list_runtime_notification_delivery_reconciliations(
            db,
            tenant_id=tenant_id,
            status="dead_letter",
            limit=10,
        )
        assert listed["authority_snapshot"]["valid"] is True
        assert listed["authority_snapshot"]["child_agent_id"] == str(target_agent_id)
        assert listed["retryable"] is True
        retried = await retry_runtime_notification_delivery(
            db,
            tenant_id=tenant_id,
            delivery_id=outbox_id,
            reason="operator verified target child authority",
            actor_user_id=user_id,
        )
        assert retried["status"] == "pending"


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.parametrize("lifecycle_field", ["deleted_at", "deactivated_at"])
async def test_generic_dead_letter_rejects_inactive_parent_agent_authority(
    owner_sessionmaker,
    lifecycle_field,
):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=user_id,
                result_summary="completed",
                metadata_json={},
            )
        )
        outbox_id = await enqueue_completion_notification(
            db,
            _notification(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                source_run_id=str(task_id),
            ),
        )
        row = await db.get(RuntimeNotificationOutbox, outbox_id, with_for_update=True)
        assert row is not None
        row.status = "dead_letter"
        agent = await db.get(Agent, agent_id, with_for_update=True)
        assert agent is not None
        setattr(agent, lifecycle_field, datetime.now(UTC))

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        [listed] = await list_runtime_notification_delivery_reconciliations(
            db,
            tenant_id=tenant_id,
            status="dead_letter",
            limit=10,
        )
        assert listed["authority_snapshot"]["valid"] is False
        assert listed["retryable"] is False
        with pytest.raises(ValueError, match="target authority"):
            await retry_runtime_notification_delivery(
                db,
                tenant_id=tenant_id,
                delivery_id=outbox_id,
                reason="operator verified inactive target authority",
                actor_user_id=user_id,
            )


@pytest.mark.usefixtures("migrated_pg_url")
async def test_a2a_dead_letter_rejects_child_session_not_owned_by_task_target(owner_sessionmaker):
    tenant_id, user_id, parent_agent_id, parent_session_id = await _seed_parent_session(owner_sessionmaker)
    target_agent_id = uuid.uuid4()
    wrong_agent_id = uuid.uuid4()
    child_session_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add_all(
            [
                Agent(
                    id=target_agent_id,
                    tenant_id=tenant_id,
                    name="Expected Target",
                    creator_id=user_id,
                    sponsor_user_id=user_id,
                ),
                Agent(
                    id=wrong_agent_id,
                    tenant_id=tenant_id,
                    name="Wrong Target",
                    creator_id=user_id,
                    sponsor_user_id=user_id,
                ),
            ]
        )
        await db.flush()
        db.add(
            ChatSession(
                id=child_session_id,
                tenant_id=tenant_id,
                agent_id=wrong_agent_id,
                user_id=user_id,
                title="Wrong A2A Child",
                source_channel="agent",
                session_kind="delegation_run",
                parent_session_id=parent_session_id,
                root_session_id=parent_session_id,
            )
        )
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="delegation",
                status="completed",
                parent_agent_id=parent_agent_id,
                child_agent_id=target_agent_id,
                parent_session_id=str(parent_session_id),
                child_session_id=str(child_session_id),
                root_user_id=user_id,
                result_summary="target completed",
                metadata_json={},
            )
        )
        outbox_id = await enqueue_completion_notification(
            db,
            CompletionNotification(
                tenant_id=tenant_id,
                source_kind="a2a_delegation",
                source_run_id=str(task_id),
                parent_session_id=parent_session_id,
                parent_agent_id=parent_agent_id,
                parent_user_id=user_id,
                child_session_id=child_session_id,
                terminal_status="completed",
                task_type="delegation",
                summary="target completed",
            ),
        )
        row = await db.get(RuntimeNotificationOutbox, outbox_id, with_for_update=True)
        assert row is not None
        row.status = "dead_letter"

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        [listed] = await list_runtime_notification_delivery_reconciliations(
            db,
            tenant_id=tenant_id,
            status="dead_letter",
            limit=10,
        )
        assert listed["authority_snapshot"]["valid"] is False
        assert listed["retryable"] is False
        with pytest.raises(ValueError, match="target authority"):
            await retry_runtime_notification_delivery(
                db,
                tenant_id=tenant_id,
                delivery_id=outbox_id,
                reason="operator rejected mismatched child authority",
                actor_user_id=user_id,
            )


@pytest.mark.usefixtures("migrated_pg_url")
async def test_delivery_rechecks_live_authority_before_invoking_parent_continuation(
    owner_sessionmaker,
    monkeypatch,
):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=user_id,
                result_summary="completed",
                metadata_json={},
            )
        )
        outbox_id = await enqueue_completion_notification(
            db,
            _notification(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                source_run_id=str(task_id),
            ),
        )
        row = await db.get(RuntimeNotificationOutbox, outbox_id, with_for_update=True)
        assert row is not None
        row.available_at = datetime(1900, 1, 1, tzinfo=UTC)
        agent = await db.get(Agent, agent_id, with_for_update=True)
        assert agent is not None
        agent.deactivated_at = datetime.now(UTC)

    continuation_called = False

    async def forbidden_continuation(**_kwargs):
        nonlocal continuation_called
        continuation_called = True
        raise AssertionError("invalid live authority must stop before parent continuation")

    monkeypatch.setattr(
        "app.services.agent_session_continuation.continue_parent_session_with_task_notification",
        forbidden_continuation,
    )
    result = await RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        max_attempts=1,
    ).drain_once(worker_id="authority-recheck", limit=1, item_ids={outbox_id})

    assert result["dead_lettered"] == 1
    assert continuation_called is False
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        row = await db.get(RuntimeNotificationOutbox, outbox_id)
        assert row is not None
        assert row.status == "dead_letter"
        assert "authority no longer resolves" in str(row.last_error)


@pytest.mark.usefixtures("migrated_pg_url")
async def test_generic_retry_service_rejects_blank_or_short_trimmed_reason(owner_sessionmaker):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=user_id,
                result_summary="completed",
                metadata_json={},
            )
        )
        outbox_id = await enqueue_completion_notification(
            db,
            _notification(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                source_run_id=str(task_id),
            ),
        )
        row = await db.get(RuntimeNotificationOutbox, outbox_id, with_for_update=True)
        assert row is not None
        row.status = "dead_letter"

    for reason in ("        ", "  short  "):
        async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
            with pytest.raises(ValueError, match="at least 8"):
                await retry_runtime_notification_delivery(
                    db,
                    tenant_id=tenant_id,
                    delivery_id=outbox_id,
                    reason=reason,
                    actor_user_id=user_id,
                )
            await db.rollback()


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reconciler_backfills_terminal_runtime_task_missing_outbox(owner_sessionmaker):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="workflow",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                result_summary="workflow result",
                metadata_json={
                    "user_id": str(user_id),
                    "parent_session_id": str(session_id),
                    "artifacts": [{"type": "artifact", "path": "workspace/result.md"}],
                },
            )
        )
        await db.commit()

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    repaired = await service.reconcile_terminal_tasks_once(limit=10, task_ids={task_id})

    async with owner_sessionmaker() as db:
        row = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(task_id))
            )
        ).scalar_one()

    assert repaired >= 1
    assert row.source_kind == "workflow"
    assert row.parent_session_id == session_id
    assert row.parent_user_id == user_id
    assert row.summary == "workflow result"
    assert row.artifacts_json == [{"type": "artifact", "path": "workspace/result.md"}]


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reconciler_normalizes_legacy_a2a_task_type_instead_of_treating_it_as_satisfied(
    owner_sessionmaker,
):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="delegation",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=user_id,
                result_summary="canonical delegation result",
                metadata_json={"user_id": str(user_id)},
            )
        )
        await enqueue_completion_notification(
            db,
            CompletionNotification(
                tenant_id=tenant_id,
                source_kind="a2a_delegation",
                source_run_id=str(task_id),
                parent_session_id=session_id,
                parent_agent_id=agent_id,
                parent_user_id=user_id,
                terminal_status="completed",
                task_type="a2a_delegation",
                summary="legacy delegation result",
            ),
        )

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    assert await service.reconcile_terminal_tasks_once(limit=10, task_ids={task_id}) == 1
    assert await service.reconcile_terminal_tasks_once(limit=10, task_ids={task_id}) == 0

    async with owner_sessionmaker() as db:
        row = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.source_kind == "a2a_delegation",
                    RuntimeNotificationOutbox.source_run_id == str(task_id),
                )
            )
        ).scalar_one()
    assert row.task_type == "delegation"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_a2a_completion_real_producer_persists_canonical_delegation_task_type(owner_sessionmaker):
    from app.agents.orchestrator import AgentDelegationRequest, _wake_parent_session_from_delegation_completion

    tenant_id, user_id, parent_agent_id, parent_session_id = await _seed_parent_session(owner_sessionmaker)
    target_agent_id = uuid.uuid4()
    child_session_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            Agent(
                id=target_agent_id,
                tenant_id=tenant_id,
                name="A2A target",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await db.flush()
        db.add(
            ChatSession(
                id=child_session_id,
                tenant_id=tenant_id,
                agent_id=target_agent_id,
                user_id=user_id,
                title="A2A child",
                source_channel="agent",
                session_kind="delegation_run",
                parent_session_id=parent_session_id,
                root_session_id=parent_session_id,
            )
        )
        await db.flush()
        request = AgentDelegationRequest(
            target=SimpleNamespace(id=target_agent_id, name="A2A target", tenant_id=tenant_id),
            target_model=SimpleNamespace(),
            conversation_messages=[{"role": "user", "content": "delegate"}],
            owner_id=user_id,
            session_id=str(child_session_id),
            parent_agent_id=parent_agent_id,
            parent_session_id=str(parent_session_id),
            trace_id="real-producer-contract",
            depth=1,
            tenant_id=tenant_id,
            runtime_task_id=str(task_id),
        )
        await _wake_parent_session_from_delegation_completion(
            db=db,
            request=request,
            task_id=str(task_id),
            status="completed",
            summary="producer result",
        )

    async with owner_sessionmaker() as db:
        row = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.source_kind == "a2a_delegation",
                    RuntimeNotificationOutbox.source_run_id == str(task_id),
                )
            )
        ).scalar_one()
    assert row.task_type == "delegation"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_expired_delegation_reconciliation_sweep_and_drain_deliver_one_canonical_continuation(
    owner_sessionmaker,
):
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="delegation",
                status="running",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=user_id,
                claimed_by="expired-delegation-worker",
                claim_version=4,
                claim_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                result_summary="expired delegation",
                metadata_json={
                    "resume_after_restart": True,
                    "resumable_delegation": True,
                    "side_effect_risk": "mutating",
                    "tool_profile": "worker_safe",
                },
            )
        )
        await db.commit()

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        claimed = await RuntimeTaskClaimService(
            db=db,
            worker_id="recovery-worker",
            task_types=("delegation",),
            lease_seconds=60,
        ).claim_available(batch_size=1)
    assert claimed == []

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    assert await service.reconcile_terminal_tasks_once(limit=10, task_ids={task_id}) == 0
    deliveries = []

    async def deliver(item):
        deliveries.append(item)
        return {"continuation_run_id": "one"}

    async with owner_sessionmaker() as db:
        canonical_outbox_id = (
            await db.execute(
                select(RuntimeNotificationOutbox.id).where(
                    RuntimeNotificationOutbox.tenant_id == tenant_id,
                    RuntimeNotificationOutbox.source_run_id == str(task_id),
                )
            )
        ).scalar_one()
    first = await service.drain_once(
        worker_id="canonical-a2a-worker",
        deliver=deliver,
        item_ids={canonical_outbox_id},
    )
    second = await service.drain_once(
        worker_id="canonical-a2a-worker-repeat",
        deliver=deliver,
        item_ids={canonical_outbox_id},
    )

    async with owner_sessionmaker() as db:
        rows = list(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(
                        RuntimeNotificationOutbox.tenant_id == tenant_id,
                        RuntimeNotificationOutbox.source_run_id == str(task_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert first["delivered"] == 1
    assert second["claimed"] == 0
    assert len(deliveries) == 1
    assert len(rows) == 1
    assert rows[0].source_kind == "a2a_delegation"
    assert rows[0].task_type == "delegation"
    assert rows[0].status == "delivered"


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.parametrize("terminal_status", ["completed", "failed", "killed"])
async def test_foreground_inline_a2a_terminal_sweep_projects_once_without_parent_continuation(
    owner_sessionmaker,
    terminal_status,
):
    """A synchronous A2A result is already in the active parent model turn.

    The crash-repair sweep may durably project that terminal fact into the
    parent transcript, but it must never wake a second parent model run.
    """

    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="a2a_delegation",
                status=terminal_status,
                parent_agent_id=agent_id,
                child_agent_id=agent_id,
                parent_session_id=str(session_id),
                child_session_id=None,
                root_user_id=user_id,
                result_summary=f"inline peer {terminal_status}",
                metadata_json={
                    "execution_backend": "foreground_inline",
                    "parent_session_id": str(session_id),
                    "user_id": str(user_id),
                },
            )
        )
        await db.commit()

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    assert await service.reconcile_terminal_tasks_once(limit=10, task_ids={task_id}) == 1

    async with owner_sessionmaker() as db:
        outbox = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.source_kind == "a2a_delegation",
                    RuntimeNotificationOutbox.source_run_id == str(task_id),
                )
            )
        ).scalar_one()
        runtime_task_count_before = (await db.execute(select(func.count()).select_from(RuntimeTask))).scalar_one()

    assert outbox.delivery_mode == "session_projection"
    assert outbox.metadata_json["foreground_inline_result_already_returned"] is True

    # Compatibility fence: an outbox created by the pre-fix sweeper may still
    # carry the unsafe mode while waiting to be delivered after deployment.
    async with owner_sessionmaker() as db:
        legacy_pending = await db.get(RuntimeNotificationOutbox, outbox.id, with_for_update=True)
        assert legacy_pending is not None
        legacy_pending.delivery_mode = "parent_continuation"
        await db.commit()

    first = await service.drain_once(
        worker_id=f"a2a-projector-{terminal_status}",
        item_ids={outbox.id},
    )
    second = await service.drain_once(
        worker_id=f"a2a-projector-repeat-{terminal_status}",
        item_ids={outbox.id},
    )

    async with owner_sessionmaker() as db:
        runtime_task_count_after = (await db.execute(select(func.count()).select_from(RuntimeTask))).scalar_one()
        projected_events = (
            await db.execute(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.causation_id == outbox.id,
                    ChatTranscriptEvent.event_type == "agent_task_notification",
                )
            )
        ).scalar_one()

    assert first["delivered"] == 1
    assert second["claimed"] == 0
    assert runtime_task_count_after == runtime_task_count_before
    assert projected_events == 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_reconciler_backfills_terminal_approval_continuation(owner_sessionmaker):
    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    task_id = uuid.uuid4()
    approval_id = uuid.uuid4()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="approval_execution",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=user_id,
                result_summary="approved file result",
                metadata_json={"approval_id": str(approval_id), "tool_name": "write_file"},
            )
        )
        await db.commit()

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    repaired = await service.reconcile_terminal_tasks_once(limit=10, task_ids={task_id})

    async with owner_sessionmaker() as db:
        row = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(task_id))
            )
        ).scalar_one()
    assert repaired >= 1
    assert row.source_kind == "approval"
    assert row.task_type == "approval_execution"
    assert row.parent_session_id == session_id
    assert row.parent_user_id == user_id
    assert row.metadata_json["approval_id"] == str(approval_id)


@pytest.mark.usefixtures("migrated_pg_url")
async def test_parent_continuation_waits_for_budget_approval_then_resumes(owner_sessionmaker):
    from app.services.runtime_budget_service import RuntimeBudgetRunCreate, RuntimeBudgetService

    tenant_id, user_id, agent_id, session_id = await _seed_parent_session(owner_sessionmaker)
    budget_service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    budget_run = await budget_service.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=tenant_id,
            root_run_kind="web_chat_turn",
            root_run_key="outbox-budget",
            source="web",
            profile="interactive",
            enforcement_mode="enforce",
            fail_mode="require_confirmation",
            max_continuation_wakes=0,
        )
    )
    task_id = uuid.uuid4()
    notification = CompletionNotification(
        tenant_id=tenant_id,
        source_kind="subagent",
        source_run_id=str(task_id),
        parent_session_id=session_id,
        parent_agent_id=agent_id,
        parent_user_id=user_id,
        terminal_status="completed",
        task_type="subagent",
        summary="budgeted result",
        delivery_mode="parent_continuation",
        metadata={"budget_run_id": str(budget_run.id)},
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=user_id,
                result_summary="budgeted result",
                metadata_json={"budget_run_id": str(budget_run.id)},
            )
        )
        outbox_id = await enqueue_completion_notification(db, notification)
        await db.commit()

    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        deferred_retry_seconds=0,
    )
    waiting = await service.drain_once(worker_id="worker-a", item_ids={outbox_id})
    async with owner_sessionmaker() as db:
        pending = (
            await db.execute(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == outbox_id))
        ).scalar_one()
    assert waiting["deferred"] == 1
    assert pending.status == "pending"
    assert pending.last_error == "runtime_budget_approval_required"

    await budget_service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=budget_run.id,
        reason="owner approved completion synthesis",
        actor_user_id=user_id,
        max_continuation_wakes=1,
    )
    resumed = await service.drain_once(worker_id="worker-b", item_ids={outbox_id})

    async with owner_sessionmaker() as db:
        delivered = (
            await db.execute(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == outbox_id))
        ).scalar_one()
        event_count = (
            await db.execute(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.causation_id == outbox_id,
                    ChatTranscriptEvent.event_type == "agent_task_notification",
                )
            )
        ).scalar_one()
    assert resumed["delivered"] == 1
    assert delivered.status == "delivered"
    assert event_count == 1
