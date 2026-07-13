from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import delete, func, select, text, update

from app.agents.subagent import SubagentJob, SubagentSpec, _spawn_one
from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.runtime.invoker import AgentInvocationResult
from app.services import subagent_run_service as subagent_service
from app.services.runtime_budget_service import (
    RuntimeBudgetReservation,
    RuntimeBudgetRunCreate,
    RuntimeBudgetService,
    RuntimeBudgetSettlement,
)
from app.services.runtime_notification_outbox import (
    CompletionNotification,
    RuntimeNotificationOutboxService,
    enqueue_completion_notification,
    list_runtime_notification_delivery_reconciliations,
    retry_runtime_notification_delivery,
)
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest


@pytest.fixture
async def budget_artifact_cleanup(owner_sessionmaker):
    """Remove only the RuntimeTask/budget artifacts created by one race test."""

    tracked = {"run_ids": set(), "task_ids": set(), "outbox_ids": set()}
    try:
        yield tracked
    finally:
        async with owner_sessionmaker() as db:
            if tracked["outbox_ids"]:
                await db.execute(
                    delete(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id.in_(tracked["outbox_ids"]))
                )
            if tracked["task_ids"]:
                await db.execute(delete(RuntimeTask).where(RuntimeTask.id.in_(tracked["task_ids"])))
            if tracked["run_ids"]:
                await db.execute(
                    delete(RuntimeBudgetEvent).where(RuntimeBudgetEvent.budget_run_id.in_(tracked["run_ids"]))
                )
                await db.execute(delete(RuntimeBudgetRun).where(RuntimeBudgetRun.id.in_(tracked["run_ids"])))
            await db.commit()


async def _seed_subagent_authority(owner_sessionmaker, *, disabled_primary: bool = False):
    tenant_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    root_user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    child_session_id = uuid.uuid4()
    primary_model_id = uuid.uuid4()
    fallback_model_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Subagent Authority", slug=f"subauth-{tenant_id.hex[:10]}"))
        await db.flush()
        db.add_all(
            [
                User(
                    id=creator_id,
                    username=f"creator-{creator_id.hex[:10]}",
                    email=f"{creator_id.hex[:10]}@subauth.test",
                    password_hash="x",
                    display_name="Creator A",
                    tenant_id=tenant_id,
                ),
                User(
                    id=root_user_id,
                    username=f"root-{root_user_id.hex[:10]}",
                    email=f"{root_user_id.hex[:10]}@subauth.test",
                    password_hash="x",
                    display_name="Root B",
                    tenant_id=tenant_id,
                ),
                LLMModel(
                    id=primary_model_id,
                    tenant_id=tenant_id,
                    provider="openai",
                    model="primary-model",
                    api_key_encrypted="test",
                    label="Primary",
                    enabled=not disabled_primary,
                ),
                LLMModel(
                    id=fallback_model_id,
                    tenant_id=tenant_id,
                    provider="openai",
                    model="fallback-model",
                    api_key_encrypted="test",
                    label="Fallback",
                    enabled=True,
                ),
            ]
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Authority Agent",
                creator_id=creator_id,
                sponsor_user_id=creator_id,
                primary_model_id=primary_model_id,
                fallback_model_id=fallback_model_id,
            )
        )
        await db.flush()
        db.add_all(
            [
                ChatSession(
                    id=parent_session_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    user_id=root_user_id,
                    title="Parent",
                    source_channel="web",
                ),
                ChatSession(
                    id=child_session_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    user_id=root_user_id,
                    title="Child",
                    source_channel="subagent",
                    session_kind="subagent",
                    parent_session_id=parent_session_id,
                    root_session_id=parent_session_id,
                ),
            ]
        )
        await db.commit()
    return SimpleNamespace(
        tenant_id=tenant_id,
        creator_id=creator_id,
        root_user_id=root_user_id,
        agent_id=agent_id,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        primary_model_id=primary_model_id,
        fallback_model_id=fallback_model_id,
    )


@pytest.fixture(autouse=True)
async def _cleanup_seeded_subagent_authority_outboxes(owner_sessionmaker, monkeypatch):
    """Remove only outboxes emitted for tenants seeded by the current test."""

    tracked_tenant_ids: set[uuid.UUID] = set()
    seed_authority = _seed_subagent_authority

    async def tracked_seed_authority(*args, **kwargs):
        authority = await seed_authority(*args, **kwargs)
        tracked_tenant_ids.add(authority.tenant_id)
        return authority

    monkeypatch.setattr(sys.modules[__name__], "_seed_subagent_authority", tracked_seed_authority)
    try:
        yield
    finally:
        if tracked_tenant_ids:
            async with owner_sessionmaker() as db:
                await db.execute(
                    delete(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.tenant_id.in_(tracked_tenant_ids))
                )
                await db.commit()
            async with owner_sessionmaker() as db:
                remaining = int(
                    (
                        await db.execute(
                            select(func.count(RuntimeNotificationOutbox.id)).where(
                                RuntimeNotificationOutbox.tenant_id.in_(tracked_tenant_ids)
                            )
                        )
                    ).scalar_one()
                )
                assert remaining == 0


@pytest.mark.usefixtures("migrated_pg_url")
async def test_background_resume_restores_root_user_for_llm_and_tool_authority(
    owner_sessionmaker,
    monkeypatch,
    tmp_path: Path,
):
    authority = await _seed_subagent_authority(owner_sessionmaker)

    runtime = await subagent_service._resolve_parent_runtime(
        authority.agent_id,
        tenant_id=authority.tenant_id,
        root_user_id=authority.root_user_id,
        parent_session_id=authority.parent_session_id,
        child_session_id=authority.child_session_id,
        session_factory=owner_sessionmaker,
    )

    assert runtime is not None
    assert runtime.parent_user_id == authority.root_user_id
    assert runtime.parent_user_id != authority.creator_id
    runtime.parent_session_id = str(authority.parent_session_id)
    runtime.child_session_id = str(authority.child_session_id)

    captured: dict[str, object] = {}

    async def external_llm_double(request):
        # Test Double rationale: the provider API is outside the process; this
        # assertion verifies the exact invocation authority sent to that boundary.
        captured["llm_user_id"] = request.user_id
        assert request.tool_executor is not None
        await request.tool_executor("read_file", {"path": "workspace/input.txt"})
        return AgentInvocationResult(content="done", tokens_used=7)

    async def governed_tool_boundary(tool_name, arguments, **kwargs):
        # Test Double rationale: file-tool execution is an external side-effect
        # boundary; ToolRuntimeService receives the user_id captured here.
        captured["tool_name"] = tool_name
        captured["tool_user_id"] = kwargs["user_id"]
        return "ok"

    monkeypatch.setattr("app.services.agent_tools.execute_tool", governed_tool_boundary)
    monkeypatch.setattr(
        "app.agents.subagent.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        raising=False,
    )

    result = await _spawn_one(
        runtime,
        SubagentJob(
            spec=SubagentSpec(name="authority-check", type="explorer", isolation="worktree"),
            task="read the input",
        ),
        invoke=external_llm_double,
    )

    assert result.ok is True
    assert captured == {
        "llm_user_id": authority.root_user_id,
        "tool_name": "read_file",
        "tool_user_id": authority.root_user_id,
    }


@pytest.mark.usefixtures("migrated_pg_url")
async def test_background_resume_fails_loud_when_configured_primary_is_disabled(owner_sessionmaker):
    authority = await _seed_subagent_authority(owner_sessionmaker, disabled_primary=True)

    with pytest.raises(subagent_service.SubagentRuntimeAuthorityError) as exc_info:
        await subagent_service._resolve_parent_runtime(
            authority.agent_id,
            tenant_id=authority.tenant_id,
            root_user_id=authority.root_user_id,
            parent_session_id=authority.parent_session_id,
            child_session_id=authority.child_session_id,
            session_factory=owner_sessionmaker,
        )

    assert exc_info.value.blocker == "primary_model_unavailable"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_subagent_terminal_budget_intent_recovers_actual_foreground_tokens_exactly_once(
    owner_sessionmaker,
    budget_artifact_cleanup,
):
    authority = await _seed_subagent_authority(owner_sessionmaker)
    budget = RuntimeBudgetService(session_factory=owner_sessionmaker)
    budget_run = await budget.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=authority.tenant_id,
            root_run_kind="web_chat_turn",
            root_run_key=f"subagent-budget-{uuid.uuid4()}",
            max_tokens=100_000,
            max_cache_miss_tokens=100_000,
            max_subagents=4,
            max_background_tasks=4,
        )
    )
    budget_artifact_cleanup["run_ids"].add(budget_run.id)
    task_id = uuid.uuid4()
    budget_artifact_cleanup["task_ids"].add(task_id)
    reservation_key = f"subagent:{task_id.hex}:start"
    await budget.reserve(
        RuntimeBudgetReservation(
            budget_run_id=budget_run.id,
            reservation_key=reservation_key,
            tokens=500,
            cache_miss_tokens=500,
            subagents=1,
            runtime_task_id=task_id,
            reason="subagent_start",
        )
    )
    intent = subagent_service._build_subagent_budget_settlement_intent(
        run_id=task_id.hex,
        record={
            "budget_run_id": str(budget_run.id),
            "budget_reservation_key": reservation_key,
            "metadata": {"execution_backend": "foreground_inline"},
        },
        status="completed",
        tokens_used=137,
    )
    assert intent is not None
    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=authority.tenant_id,
                task_type="subagent",
                status="completed",
                parent_agent_id=authority.agent_id,
                parent_session_id=str(authority.parent_session_id),
                child_session_id=str(authority.child_session_id),
                root_user_id=authority.root_user_id,
                budget_run_id=budget_run.id,
                budget_reservation_key=reservation_key,
                metadata_json={
                    "execution_backend": "foreground_inline",
                    "budget_settlement_intent": intent,
                },
                token_usage={"total_tokens": 137},
                completed_at=datetime.now(UTC),
            )
        )
        await db.commit()

    await budget.reconcile_orphaned_reservations()
    await budget.reconcile_orphaned_reservations()

    async with owner_sessionmaker() as db:
        settlements = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == budget_run.id,
                        RuntimeBudgetEvent.reservation_key == reservation_key,
                        RuntimeBudgetEvent.event_type == "settlement",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(settlements) == 1
    assert settlements[0].amounts_json == {
        "tokens": 137,
        "cache_miss_tokens": 137,
        "subagents": 1,
    }
    assert settlements[0].runtime_task_id == task_id
    assert settlements[0].metadata_json["settlement_intent_schema"] == "subagent_budget_settlement_intent.v1"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_subagent_dead_letter_is_delivery_only_and_retries_once_after_authority_repair(
    owner_sessionmaker,
    budget_artifact_cleanup,
):
    authority = await _seed_subagent_authority(owner_sessionmaker)
    wrong_user_id = authority.creator_id
    task_id = uuid.uuid4()
    budget_artifact_cleanup["task_ids"].add(task_id)
    oldest = datetime(1900, 1, 1, tzinfo=UTC)
    notification = CompletionNotification(
        tenant_id=authority.tenant_id,
        source_kind="subagent",
        source_run_id=str(task_id),
        parent_session_id=authority.parent_session_id,
        parent_agent_id=authority.agent_id,
        parent_user_id=wrong_user_id,
        child_session_id=authority.child_session_id,
        child_agent_name="worker",
        terminal_status="completed",
        task_type="subagent",
        summary="already executed",
        delivery_mode="parent_continuation",
        metadata={"subagent_terminal_projection_required": False},
    )
    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=authority.tenant_id,
                task_type="subagent",
                status="completed",
                parent_agent_id=authority.agent_id,
                parent_session_id=str(authority.parent_session_id),
                child_session_id=str(authority.child_session_id),
                root_user_id=wrong_user_id,
                result_summary="already executed",
                metadata_json={"execution_backend": "runtime_task_worker"},
                completed_at=datetime.now(UTC),
            )
        )
        outbox_id = await enqueue_completion_notification(db, notification)
        outbox = await db.get(RuntimeNotificationOutbox, outbox_id, with_for_update=True)
        assert outbox is not None
        outbox.available_at = oldest
        outbox.updated_at = oldest
        await db.commit()
    budget_artifact_cleanup["outbox_ids"].add(outbox_id)

    service = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=1,
    )
    failed = await service.drain_once(worker_id="delivery-a", limit=1)
    async with owner_sessionmaker() as db:
        dead = await db.get(RuntimeNotificationOutbox, outbox_id)
        task = await db.get(RuntimeTask, task_id)
        assert dead is not None and task is not None
        assert dead.status == "dead_letter"
        assert dead.metadata_json["delivery_reconciliation"]["delivery_only"] is True
        assert dead.metadata_json["delivery_reconciliation"]["status"] == "needs_reconciliation"
        assert task.status == "completed"
    assert failed["dead_lettered"] == 1

    # The generic terminal sweep must not consider dead-letter delivery satisfied,
    # and an empty task-id filter must never broaden into a full-table repair.
    assert await service.reconcile_terminal_tasks_once(task_ids=set()) == 0

    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        parent = await db.get(ChatSession, authority.parent_session_id)
        task = await db.get(RuntimeTask, task_id)
        assert parent is not None and task is not None
        parent.user_id = wrong_user_id
        child = await db.get(ChatSession, authority.child_session_id)
        assert child is not None
        child.user_id = wrong_user_id
        await db.commit()

    async with owner_sessionmaker() as db:
        dead = await db.get(RuntimeNotificationOutbox, outbox_id, with_for_update=True)
        assert dead is not None
        dead.updated_at = oldest
        await db.commit()

    retried = await service.retry_recoverable_dead_letters_once(limit=1)
    deliveries: list[uuid.UUID] = []

    async def delivery_boundary(item):
        deliveries.append(item.id)
        return {"status": "delivered", "delivery_only": True}

    async with owner_sessionmaker() as db:
        pending = await db.get(RuntimeNotificationOutbox, outbox_id, with_for_update=True)
        assert pending is not None
        pending.available_at = oldest
        await db.commit()

    first = await service.drain_once(worker_id="delivery-b", deliver=delivery_boundary, limit=1)

    async with owner_sessionmaker() as db:
        delivered = await db.get(RuntimeNotificationOutbox, outbox_id)
        task = await db.get(RuntimeTask, task_id)
    assert retried == 1
    assert first["delivered"] == 1
    assert deliveries == [outbox_id]
    assert delivered is not None and delivered.status == "delivered"
    assert delivered.delivery_receipt_json["delivery_only"] is True
    assert task is not None and task.status == "completed"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_generic_delivery_failure_with_valid_authority_is_not_automatically_requeued(
    owner_sessionmaker,
    budget_artifact_cleanup,
):
    authority = await _seed_subagent_authority(owner_sessionmaker)
    task_id = uuid.uuid4()
    budget_artifact_cleanup["task_ids"].add(task_id)
    oldest = datetime(1900, 1, 1, tzinfo=UTC)
    notification = CompletionNotification(
        tenant_id=authority.tenant_id,
        source_kind="subagent",
        source_run_id=str(task_id),
        parent_session_id=authority.parent_session_id,
        parent_agent_id=authority.agent_id,
        parent_user_id=authority.root_user_id,
        child_session_id=authority.child_session_id,
        child_agent_name="worker",
        terminal_status="completed",
        task_type="subagent",
        summary="already executed",
        delivery_mode="parent_continuation",
        metadata={"subagent_terminal_projection_required": False},
    )
    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=authority.tenant_id,
                task_type="subagent",
                status="completed",
                parent_agent_id=authority.agent_id,
                parent_session_id=str(authority.parent_session_id),
                child_session_id=str(authority.child_session_id),
                root_user_id=authority.root_user_id,
                result_summary="already executed",
                metadata_json={"execution_backend": "runtime_task_worker"},
                completed_at=datetime.now(UTC),
            )
        )
        outbox_id = await enqueue_completion_notification(db, notification)
        outbox = await db.get(RuntimeNotificationOutbox, outbox_id, with_for_update=True)
        assert outbox is not None
        outbox.available_at = oldest
        await db.commit()
    budget_artifact_cleanup["outbox_ids"].add(outbox_id)

    async def generic_failure(_item):
        raise RuntimeError("provider unavailable")

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker, max_attempts=1)
    failed = await service.drain_once(worker_id="generic-a", deliver=generic_failure, limit=1)
    await service.retry_recoverable_dead_letters_once(limit=10)

    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeNotificationOutbox, outbox_id)
    assert failed["dead_lettered"] == 1
    assert row is not None and row.status == "dead_letter"
    assert row.metadata_json["delivery_reconciliation"]["failure_kind"] == "delivery_failure"
    assert row.metadata_json["delivery_reconciliation"]["authority_snapshot"]["valid"] is True

    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        listed = await list_runtime_notification_delivery_reconciliations(
            db,
            tenant_id=authority.tenant_id,
            status="dead_letter",
            limit=10,
        )
        retried = await retry_runtime_notification_delivery(
            db,
            tenant_id=authority.tenant_id,
            delivery_id=outbox_id,
            reason="operator verified the delivery boundary failure",
            actor_user_id=authority.creator_id,
        )

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, task_id)
        pending = await db.get(RuntimeNotificationOutbox, outbox_id)
        audit = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.tenant_id == authority.tenant_id,
                    AuditLog.action == "runtime_notification_delivery_retry",
                )
            )
        ).scalar_one()
    listed_item = next(item for item in listed if item["delivery_id"] == str(outbox_id))
    assert listed_item["delivery_only"] is True
    assert listed_item["execution_terminal_status"] == "completed"
    assert retried["status"] == "pending"
    assert retried["delivery_only"] is True
    assert task is not None and task.status == "completed"
    assert pending is not None and pending.status == "pending"
    assert audit.details["delivery_only"] is True
    assert audit.details["does_not_rerun_execution"] is True


@pytest.mark.usefixtures("migrated_pg_url")
async def test_empty_task_id_filter_does_not_reconcile_unrelated_terminal_tasks(owner_sessionmaker):
    authority = await _seed_subagent_authority(owner_sessionmaker)
    task_id = uuid.uuid4()
    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=authority.tenant_id,
                task_type="subagent",
                status="completed",
                parent_agent_id=authority.agent_id,
                parent_session_id=str(authority.parent_session_id),
                child_session_id=str(authority.child_session_id),
                root_user_id=authority.root_user_id,
                result_summary="unrelated",
                metadata_json={"execution_backend": "runtime_task_worker"},
                completed_at=datetime.now(UTC),
            )
        )
        await db.commit()

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    repaired = await service.reconcile_terminal_tasks_once(task_ids=set())

    async with owner_sessionmaker() as db:
        count = (
            await db.execute(
                select(func.count())
                .select_from(RuntimeNotificationOutbox)
                .where(RuntimeNotificationOutbox.source_run_id == str(task_id))
            )
        ).scalar_one()
    assert repaired == 0
    assert count == 0


@pytest.mark.asyncio
async def test_restart_metadata_missing_uses_unified_terminal_cas_and_recovery_intents(monkeypatch):
    run_id = uuid.uuid4().hex
    tenant_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    root_user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    child_session_id = uuid.uuid4()
    budget_run_id = uuid.uuid4()
    updates: list[dict] = []
    record = {
        "task_id": run_id,
        "task_type": "subagent",
        "status": "running",
        "tenant_id": str(tenant_id),
        "parent_agent_id": str(parent_agent_id),
        "root_user_id": str(root_user_id),
        "parent_session_id": str(parent_session_id),
        "child_session_id": str(child_session_id),
        "budget_run_id": str(budget_run_id),
        "budget_reservation_key": f"subagent:{run_id}:start",
        "metadata": {
            "subagent_name": "worker",
            "subagent_type": "worker",
            "execution_backend": "runtime_task_worker",
        },
    }

    async def get_record(_run_id):
        return record

    async def update_record(_run_id, **fields):
        updates.append(fields)
        return True

    async def no_projection(_notification):
        return None

    async def no_settlement(_intent):
        return None

    monkeypatch.setattr(subagent_service, "get_runtime_task_record", get_record)
    monkeypatch.setattr(subagent_service, "update_runtime_task_record", update_record)
    monkeypatch.setattr(subagent_service, "_project_subagent_completion_notification", no_projection)
    monkeypatch.setattr(subagent_service, "_settle_subagent_budget_intent", no_settlement)

    dispatched = await subagent_service.dispatch_persisted_subagent_run(run_id)

    terminal = updates[-1]
    assert dispatched is True
    assert terminal["expected_status"] == ("pending", "running", "resumable")
    assert terminal["status"] == "failed"
    assert terminal["completion_notification"].terminal_status == "failed"
    assert terminal["metadata_json"]["subagent_decision_entry"]["blocker"] == "restart_metadata_missing"
    assert terminal["metadata_json"]["budget_settlement_intent"]["runtime_task_id"] == str(uuid.UUID(run_id))
    assert terminal["metadata_json"]["budget_settlement_intent"]["actual_usage"] == {
        "tokens": 0,
        "cache_miss_tokens": 0,
        "subagents": 1,
        "background_tasks": 1,
    }


@pytest.mark.asyncio
async def test_parent_runtime_authority_failure_uses_reconciliation_terminal_cas(monkeypatch):
    run_id = uuid.uuid4().hex
    tenant_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    root_user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    child_session_id = uuid.uuid4()
    updates: list[dict] = []
    record = {
        "task_id": run_id,
        "task_type": "subagent",
        "status": "running",
        "tenant_id": str(tenant_id),
        "parent_agent_id": str(parent_agent_id),
        "root_user_id": str(root_user_id),
        "parent_session_id": str(parent_session_id),
        "child_session_id": str(child_session_id),
        "prompt": "continue",
        "metadata": {
            "subagent_name": "worker",
            "subagent_type": "explorer",
            "execution_backend": "runtime_task_worker",
            "resume_after_restart": True,
            "resumable_subagent": True,
        },
    }

    async def get_record(_run_id):
        return record

    async def unavailable(*_args, **_kwargs):
        raise subagent_service.SubagentRuntimeAuthorityError(
            "primary_model_unavailable",
            "configured primary model is unavailable",
        )

    async def update_record(_run_id, **fields):
        updates.append(fields)
        return True

    async def no_projection(_notification):
        return None

    monkeypatch.setattr(subagent_service, "get_runtime_task_record", get_record)
    monkeypatch.setattr(subagent_service, "_resolve_parent_runtime", unavailable)
    monkeypatch.setattr(subagent_service, "update_runtime_task_record", update_record)
    monkeypatch.setattr(subagent_service, "_project_subagent_completion_notification", no_projection)

    dispatched = await subagent_service.dispatch_persisted_subagent_run(run_id)

    terminal = updates[-1]
    assert dispatched is True
    assert terminal["status"] == "needs_reconciliation"
    assert terminal["completion_notification"].terminal_status == "needs_reconciliation"
    assert terminal["metadata_json"]["subagent_decision_entry"]["blocker"] == "primary_model_unavailable"
    assert terminal["metadata_json"]["needs_reconciliation"] is True


def _tool_request(
    authority,
    *,
    tool_name: str,
    arguments: dict,
    budget_run_id: uuid.UUID | None = None,
) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        tool_name=tool_name,
        arguments=arguments,
        context=ToolExecutionContext(
            agent_id=authority.agent_id,
            user_id=authority.root_user_id,
            tenant_id=str(authority.tenant_id),
            workspace=Path("/tmp"),
            session_id=str(authority.parent_session_id),
            budget_run_id=str(budget_run_id) if budget_run_id is not None else None,
        ),
    )


async def _route_subagent_real_pg(monkeypatch, owner_sessionmaker) -> None:
    """Inject the migrated Testcontainers session factory without replacing domain behavior."""

    from app.services import runtime_budget_service, runtime_notification_outbox, runtime_task_service, tenant_resolver
    from app.tools.handlers import subagent as subagent_handler

    for module in (
        subagent_handler,
        subagent_service,
        runtime_budget_service,
        runtime_notification_outbox,
        runtime_task_service,
        tenant_resolver,
    ):
        if hasattr(module, "async_session"):
            monkeypatch.setattr(module, "async_session", owner_sessionmaker)
    original_tenant_scoped_session = tenant_scoped_session

    def scoped_session(tenant_id=None, **kwargs):
        kwargs.setdefault("session_factory", owner_sessionmaker)
        return original_tenant_scoped_session(tenant_id, **kwargs)

    monkeypatch.setattr(subagent_service, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(runtime_task_service, "tenant_scoped_session", scoped_session)
    # The shared checkout can gain nullable columns after the session-scoped
    # bootstrap fixture stamped its head. Refresh only this throwaway container.
    async with owner_sessionmaker() as db:
        await db.execute(text("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS external_principal_id UUID"))
        await db.execute(
            text("ALTER TABLE runtime_budget_runs ADD COLUMN IF NOT EXISTS root_external_principal_id UUID")
        )
        for ddl in (
            "ALTER TABLE runtime_tasks ADD COLUMN IF NOT EXISTS root_user_id UUID",
            "ALTER TABLE runtime_tasks ADD COLUMN IF NOT EXISTS root_session_id VARCHAR(512)",
            "ALTER TABLE runtime_tasks ADD COLUMN IF NOT EXISTS delegation_chain_json JSON DEFAULT '[]'",
            "ALTER TABLE runtime_tasks ADD COLUMN IF NOT EXISTS claim_version INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE runtime_tasks ADD COLUMN IF NOT EXISTS root_idempotency_key VARCHAR(200)",
            "ALTER TABLE runtime_tasks ADD COLUMN IF NOT EXISTS config_snapshot_hash VARCHAR(64)",
            "ALTER TABLE runtime_tasks ADD COLUMN IF NOT EXISTS policy_snapshot_hash VARCHAR(64)",
            "ALTER TABLE runtime_tasks ADD COLUMN IF NOT EXISTS budget_run_id UUID",
            "ALTER TABLE runtime_tasks ADD COLUMN IF NOT EXISTS root_runtime_task_id UUID",
            "ALTER TABLE runtime_tasks ADD COLUMN IF NOT EXISTS budget_reservation_key VARCHAR(200)",
            "ALTER TABLE runtime_tasks ADD COLUMN IF NOT EXISTS budget_admission_status VARCHAR(40)",
            "ALTER TABLE runtime_tasks ADD COLUMN IF NOT EXISTS budget_terminal_reason TEXT",
        ):
            await db.execute(text(ddl))
        await db.commit()


@pytest.mark.usefixtures("migrated_pg_url")
async def test_foreground_handler_fails_loud_for_disabled_primary_before_execution(owner_sessionmaker, monkeypatch):
    """Real handler coverage: do not replace its model/authority resolver."""

    from app.tools.handlers import subagent as subagent_handler

    await _route_subagent_real_pg(monkeypatch, owner_sessionmaker)
    authority = await _seed_subagent_authority(owner_sessionmaker, disabled_primary=True)
    payload = json.loads(
        await subagent_handler.spawn_subagent_tool(
            _tool_request(
                authority,
                tool_name="spawn_subagent",
                arguments={"task": "inspect", "type": "explorer", "model": "missing-model"},
            )
        )
    )

    assert payload["ok"] is False, payload
    assert payload.get("error_code") == "primary_model_unavailable", payload


@pytest.mark.usefixtures("migrated_pg_url")
async def test_foreground_handler_binds_budget_to_runtime_task_and_settles_once(owner_sessionmaker, monkeypatch):
    """The missing model override stops before an external provider call but uses the real handler shell."""

    from app.tools.handlers import subagent as subagent_handler

    await _route_subagent_real_pg(monkeypatch, owner_sessionmaker)
    authority = await _seed_subagent_authority(owner_sessionmaker)
    budget = RuntimeBudgetService(session_factory=owner_sessionmaker)
    budget_run = await budget.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=authority.tenant_id,
            root_run_kind="web_chat_turn",
            root_run_key=f"foreground-handler-{uuid.uuid4()}",
            max_tokens=100_000,
            max_cache_miss_tokens=100_000,
            max_subagents=4,
            max_background_tasks=4,
        )
    )

    payload = json.loads(
        await subagent_handler.spawn_subagent_tool(
            _tool_request(
                authority,
                tool_name="spawn_subagent",
                arguments={"task": "inspect", "type": "explorer", "model": "missing-model"},
                budget_run_id=budget_run.id,
            )
        )
    )
    assert payload.get("mode") == "foreground", payload
    runtime_task_id = uuid.UUID(payload["run_id"])

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, runtime_task_id)
        events = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == budget_run.id,
                        RuntimeBudgetEvent.runtime_task_id == runtime_task_id,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert task is not None
    assert task.budget_run_id == budget_run.id
    assert task.budget_reservation_key == f"subagent:{runtime_task_id.hex}:start"
    assert task.metadata_json["budget_settlement_intent"]["runtime_task_id"] == str(runtime_task_id)
    assert [event.event_type for event in events] == ["reservation", "settlement"]
    assert events[0].reservation_key == events[1].reservation_key == task.budget_reservation_key


@pytest.mark.usefixtures("migrated_pg_url")
async def test_background_reservation_without_runtime_task_is_compensated_after_grace(owner_sessionmaker):
    authority = await _seed_subagent_authority(owner_sessionmaker)
    budget = RuntimeBudgetService(session_factory=owner_sessionmaker)
    budget_run = await budget.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=authority.tenant_id,
            root_run_kind="web_chat_turn",
            root_run_key=f"orphaned-background-{uuid.uuid4()}",
            max_tokens=100_000,
            max_cache_miss_tokens=100_000,
            max_subagents=4,
            max_background_tasks=4,
        )
    )
    missing_task_id = uuid.uuid4()
    reservation_key = f"subagent:{missing_task_id.hex}:start"
    reserved_at = datetime.now(UTC)
    await budget.reserve(
        RuntimeBudgetReservation(
            budget_run_id=budget_run.id,
            reservation_key=reservation_key,
            tokens=500,
            cache_miss_tokens=500,
            subagents=1,
            background_tasks=1,
            runtime_task_id=missing_task_id,
            reason="subagent_start",
        )
    )

    before_grace = await budget.reconcile_orphaned_reservations(
        now=reserved_at,
        missing_task_grace_seconds=300,
    )
    after_grace = await budget.reconcile_orphaned_reservations(
        now=reserved_at.replace(year=reserved_at.year + 1),
        missing_task_grace_seconds=300,
    )
    duplicate = await budget.reconcile_orphaned_reservations(
        now=reserved_at.replace(year=reserved_at.year + 1),
        missing_task_grace_seconds=300,
    )

    async with owner_sessionmaker() as db:
        settlements = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == budget_run.id,
                        RuntimeBudgetEvent.reservation_key == reservation_key,
                        RuntimeBudgetEvent.event_type == "settlement",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert before_grace == 0
    assert after_grace == 1
    assert duplicate == 0
    assert len(settlements) == 1
    assert settlements[0].reason == "runtime_task_missing_after_grace"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_orphan_budget_reconcilers_skip_locked_prefix_and_settle_each_reservation_once(
    owner_sessionmaker,
):
    """Two workers must make progress without racing the same compensation row.

    The first worker is deliberately blocked on the oldest reservation's budget
    run.  The second must skip that locked event, settle the remaining batch, and
    return without a duplicate-key rollback.  This exercises real PostgreSQL row
    locks; the method wrapper only exposes the deterministic synchronization point.
    """

    authority = await _seed_subagent_authority(owner_sessionmaker)
    budget_runs = []
    reservation_keys = []
    for index in range(3):
        service = RuntimeBudgetService(session_factory=owner_sessionmaker)
        run = await service.create_run(
            RuntimeBudgetRunCreate(
                tenant_id=authority.tenant_id,
                root_run_kind="web_chat_turn",
                root_run_key=f"concurrent-orphan-{index}-{uuid.uuid4()}",
                max_tokens=100_000,
                max_cache_miss_tokens=100_000,
                max_subagents=4,
                max_background_tasks=4,
            )
        )
        missing_task_id = uuid.uuid4()
        reservation_key = f"subagent:{missing_task_id.hex}:start"
        await service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=run.id,
                reservation_key=reservation_key,
                tokens=500,
                cache_miss_tokens=500,
                subagents=1,
                background_tasks=1,
                runtime_task_id=missing_task_id,
                reason="subagent_start",
            )
        )
        budget_runs.append(run)
        reservation_keys.append(reservation_key)

    reconcile_now = datetime.now(UTC) + timedelta(minutes=10)
    first_service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    second_service = RuntimeBudgetService(session_factory=owner_sessionmaker)

    async with first_service._budget_session("test_orphan_reconcile_blocker") as blocker_db:
        await blocker_db.execute(
            select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == budget_runs[0].id).with_for_update()
        )
        second_count = await asyncio.wait_for(
            second_service.reconcile_orphaned_reservations(
                limit=10,
                now=reconcile_now,
                missing_task_grace_seconds=0,
            ),
            timeout=5,
        )
        assert second_count == 2
        await blocker_db.commit()

    first_count = await asyncio.wait_for(
        first_service.reconcile_orphaned_reservations(
            limit=1,
            now=reconcile_now,
            missing_task_grace_seconds=0,
        ),
        timeout=5,
    )
    assert first_count == 1
    assert (
        await RuntimeBudgetService(session_factory=owner_sessionmaker).reconcile_orphaned_reservations(
            limit=10,
            now=reconcile_now,
            missing_task_grace_seconds=0,
        )
        == 0
    )

    async with owner_sessionmaker() as db:
        settlements = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.reservation_key.in_(reservation_keys),
                        RuntimeBudgetEvent.event_type == "settlement",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(settlements) == 3
    assert {event.reservation_key for event in settlements} == set(reservation_keys)


@pytest.mark.usefixtures("migrated_pg_url")
async def test_missing_task_reconcile_rechecks_settlement_after_budget_lock_without_rolling_back_batch(
    owner_sessionmaker,
    monkeypatch,
):
    """A concurrent producer compensation wins without poisoning sibling repairs.

    A producer-side enqueue failure may already hold one budget-run barrier
    while the reconciler scans. The reconciler must skip that run, commit its
    sibling repair, and observe the producer's settlement on the next pass.
    """

    authority = await _seed_subagent_authority(owner_sessionmaker)
    sibling_service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    racing_service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    sibling_run = await sibling_service.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=authority.tenant_id,
            root_run_kind="web_chat_turn",
            root_run_key=f"orphan-sibling-{uuid.uuid4()}",
            max_tokens=100_000,
            max_cache_miss_tokens=100_000,
            max_subagents=4,
            max_background_tasks=4,
        )
    )
    racing_run = await racing_service.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=authority.tenant_id,
            root_run_kind="web_chat_turn",
            root_run_key=f"orphan-race-{uuid.uuid4()}",
            max_tokens=100_000,
            max_cache_miss_tokens=100_000,
            max_subagents=4,
            max_background_tasks=4,
        )
    )
    sibling_task_id = uuid.uuid4()
    racing_task_id = uuid.uuid4()
    sibling_key = f"subagent:{sibling_task_id.hex}:start"
    racing_key = f"subagent:{racing_task_id.hex}:start"
    await sibling_service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=sibling_run.id,
            reservation_key=sibling_key,
            tokens=500,
            cache_miss_tokens=500,
            subagents=1,
            background_tasks=1,
            runtime_task_id=sibling_task_id,
            reason="subagent_start",
        )
    )
    await racing_service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=racing_run.id,
            reservation_key=racing_key,
            tokens=500,
            cache_miss_tokens=500,
            subagents=1,
            background_tasks=1,
            runtime_task_id=racing_task_id,
            reason="subagent_start",
        )
    )
    base_time = datetime.now(UTC) - timedelta(minutes=20)
    async with owner_sessionmaker() as db:
        await db.execute(
            update(RuntimeBudgetEvent)
            .where(
                RuntimeBudgetEvent.budget_run_id == sibling_run.id,
                RuntimeBudgetEvent.reservation_key == sibling_key,
                RuntimeBudgetEvent.event_type == "reservation",
            )
            .values(created_at=base_time)
        )
        await db.execute(
            update(RuntimeBudgetEvent)
            .where(
                RuntimeBudgetEvent.budget_run_id == racing_run.id,
                RuntimeBudgetEvent.reservation_key == racing_key,
                RuntimeBudgetEvent.event_type == "reservation",
            )
            .values(created_at=base_time + timedelta(minutes=1))
        )
        await db.commit()

    reconciler = RuntimeBudgetService(session_factory=owner_sessionmaker)
    producer_lock_acquired = asyncio.Event()
    release_producer_lock = asyncio.Event()
    original_lock_run = RuntimeBudgetService._lock_run

    async def hold_producer_run_lock(self, db, budget_run_id):
        locked = await original_lock_run(self, db, budget_run_id)
        if self is racing_service and budget_run_id == racing_run.id:
            producer_lock_acquired.set()
            await release_producer_lock.wait()
        return locked

    monkeypatch.setattr(RuntimeBudgetService, "_lock_run", hold_producer_run_lock)
    producer_settlement = asyncio.create_task(
        racing_service.settle(
            RuntimeBudgetSettlement(
                budget_run_id=racing_run.id,
                reservation_key=racing_key,
                reason="subagent_enqueue_failed",
                runtime_task_id=racing_task_id,
            )
        )
    )
    await asyncio.wait_for(producer_lock_acquired.wait(), timeout=5)
    reconciled = await asyncio.wait_for(
        reconciler.reconcile_orphaned_reservations(
            limit=10,
            now=datetime.now(UTC),
            missing_task_grace_seconds=0,
        ),
        timeout=5,
    )
    release_producer_lock.set()
    await asyncio.wait_for(producer_settlement, timeout=5)

    duplicate_pass = await reconciler.reconcile_orphaned_reservations(
        limit=10,
        now=datetime.now(UTC),
        missing_task_grace_seconds=0,
    )
    async with owner_sessionmaker() as db:
        settlements = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.reservation_key.in_((sibling_key, racing_key)),
                        RuntimeBudgetEvent.event_type == "settlement",
                    )
                )
            )
            .scalars()
            .all()
        )

    assert reconciled == 1
    assert duplicate_pass == 0
    assert len(settlements) == 2
    assert {event.reservation_key: event.reason for event in settlements} == {
        sibling_key: "runtime_task_missing_after_grace",
        racing_key: "subagent_enqueue_failed",
    }


@pytest.mark.usefixtures("migrated_pg_url")
async def test_orphan_budget_locator_filters_live_prefix_before_limit(
    owner_sessionmaker,
    budget_artifact_cleanup,
):
    """Old live reservations must not hide later missing/terminal candidates."""

    authority = await _seed_subagent_authority(owner_sessionmaker)
    budget = RuntimeBudgetService(session_factory=owner_sessionmaker)
    budget_run = await budget.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=authority.tenant_id,
            root_run_kind="web_chat_turn",
            root_run_key=f"orphan-eligible-limit-{uuid.uuid4()}",
            max_tokens=100_000,
            max_cache_miss_tokens=100_000,
            max_subagents=8,
            max_background_tasks=8,
        )
    )
    budget_artifact_cleanup["run_ids"].add(budget_run.id)
    live_task_ids = [uuid.uuid4(), uuid.uuid4()]
    missing_task_id = uuid.uuid4()
    terminal_task_id = uuid.uuid4()
    task_ids = [*live_task_ids, missing_task_id, terminal_task_id]
    budget_artifact_cleanup["task_ids"].update(task_ids)
    reservation_keys = {task_id: f"subagent:{task_id.hex}:start" for task_id in task_ids}
    for task_id in task_ids:
        await budget.reserve(
            RuntimeBudgetReservation(
                budget_run_id=budget_run.id,
                reservation_key=reservation_keys[task_id],
                tokens=500,
                cache_miss_tokens=500,
                subagents=1,
                background_tasks=1,
                runtime_task_id=task_id,
                reason="subagent_start",
            )
        )

    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add_all(
            [
                RuntimeTask(
                    id=task_id,
                    tenant_id=authority.tenant_id,
                    task_type="subagent",
                    status="pending",
                    parent_agent_id=authority.agent_id,
                    root_user_id=authority.root_user_id,
                    root_session_id=str(authority.parent_session_id),
                    delegation_chain_json=[f"agent:{authority.agent_id}", "subagent:explorer"],
                    budget_run_id=budget_run.id,
                    budget_reservation_key=reservation_keys[task_id],
                )
                for task_id in live_task_ids
            ]
        )
        db.add(
            RuntimeTask(
                id=terminal_task_id,
                tenant_id=authority.tenant_id,
                task_type="subagent",
                status="completed",
                parent_agent_id=authority.agent_id,
                root_user_id=authority.root_user_id,
                root_session_id=str(authority.parent_session_id),
                delegation_chain_json=[f"agent:{authority.agent_id}", "subagent:explorer"],
                budget_run_id=budget_run.id,
                budget_reservation_key=reservation_keys[terminal_task_id],
                completed_at=datetime.now(UTC),
            )
        )
        await db.commit()

    base_time = datetime.now(UTC) - timedelta(hours=1)
    async with owner_sessionmaker() as db:
        for index, task_id in enumerate(task_ids):
            await db.execute(
                update(RuntimeBudgetEvent)
                .where(
                    RuntimeBudgetEvent.budget_run_id == budget_run.id,
                    RuntimeBudgetEvent.reservation_key == reservation_keys[task_id],
                    RuntimeBudgetEvent.event_type == "reservation",
                )
                .values(created_at=base_time + timedelta(seconds=index))
            )
        await db.commit()

    reconciled = await budget.reconcile_orphaned_reservations(
        limit=2,
        now=datetime.now(UTC),
        missing_task_grace_seconds=0,
    )

    async with owner_sessionmaker() as db:
        settlements = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == budget_run.id,
                        RuntimeBudgetEvent.event_type == "settlement",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert reconciled == 2
    assert {event.reservation_key for event in settlements} == {
        reservation_keys[missing_task_id],
        reservation_keys[terminal_task_id],
    }


@pytest.mark.usefixtures("migrated_pg_url")
async def test_orphan_budget_reconcilers_use_global_run_lock_order(
    owner_sessionmaker,
    monkeypatch,
    budget_artifact_cleanup,
):
    """Disjoint reservation rows must not produce an A->B/B->A run-lock cycle."""

    authority = await _seed_subagent_authority(owner_sessionmaker)
    runs = []
    for index in range(2):
        service = RuntimeBudgetService(session_factory=owner_sessionmaker)
        runs.append(
            await service.create_run(
                RuntimeBudgetRunCreate(
                    tenant_id=authority.tenant_id,
                    root_run_kind="web_chat_turn",
                    root_run_key=f"orphan-run-lock-order-{index}-{uuid.uuid4()}",
                    max_tokens=100_000,
                    max_cache_miss_tokens=100_000,
                    max_subagents=8,
                    max_background_tasks=8,
                )
            )
        )
    budget_artifact_cleanup["run_ids"].update(run.id for run in runs)
    run_a, run_b = sorted(runs, key=lambda run: run.id)
    task_ids_by_run = {
        run_a.id: [uuid.uuid4(), uuid.uuid4()],
        run_b.id: [uuid.uuid4(), uuid.uuid4()],
    }
    budget_artifact_cleanup["task_ids"].update(task_id for task_ids in task_ids_by_run.values() for task_id in task_ids)
    reservation_keys: dict[uuid.UUID, str] = {}
    for run in (run_a, run_b):
        service = RuntimeBudgetService(session_factory=owner_sessionmaker)
        for task_id in task_ids_by_run[run.id]:
            reservation_key = f"subagent:{task_id.hex}:start"
            reservation_keys[task_id] = reservation_key
            await service.reserve(
                RuntimeBudgetReservation(
                    budget_run_id=run.id,
                    reservation_key=reservation_key,
                    tokens=500,
                    cache_miss_tokens=500,
                    subagents=1,
                    background_tasks=1,
                    runtime_task_id=task_id,
                    reason="subagent_start",
                )
            )

    # The pre-fix created-at locator partitioned the workers as A1/B1 and B2/A2.
    # The synchronization wrapper still calls the real PostgreSQL row-lock
    # method; it only makes the otherwise timing-sensitive cycle deterministic.
    ordered_events = [
        (run_a.id, task_ids_by_run[run_a.id][0]),
        (run_b.id, task_ids_by_run[run_b.id][0]),
        (run_b.id, task_ids_by_run[run_b.id][1]),
        (run_a.id, task_ids_by_run[run_a.id][1]),
    ]
    base_time = datetime.now(UTC) - timedelta(hours=1)
    async with owner_sessionmaker() as db:
        for index, (run_id, task_id) in enumerate(ordered_events):
            await db.execute(
                update(RuntimeBudgetEvent)
                .where(
                    RuntimeBudgetEvent.budget_run_id == run_id,
                    RuntimeBudgetEvent.reservation_key == reservation_keys[task_id],
                    RuntimeBudgetEvent.event_type == "reservation",
                )
                .values(created_at=base_time + timedelta(seconds=index))
            )
        await db.commit()

    first = RuntimeBudgetService(session_factory=owner_sessionmaker)
    second = RuntimeBudgetService(session_factory=owner_sessionmaker)
    first_run_a_locked = asyncio.Event()
    second_first_lock_requested = asyncio.Event()
    second_run_b_locked = asyncio.Event()
    second_first_run_id: uuid.UUID | None = None
    original_lock_run = RuntimeBudgetService._lock_run

    async def expose_real_cross_run_lock_order(self, db, budget_run_id):
        nonlocal second_first_run_id
        if self is first and budget_run_id == run_a.id:
            locked = await original_lock_run(self, db, budget_run_id)
            first_run_a_locked.set()
            await second_first_lock_requested.wait()
            if second_first_run_id == run_b.id:
                await second_run_b_locked.wait()
            return locked
        if self is second and second_first_run_id is None:
            second_first_run_id = budget_run_id
            second_first_lock_requested.set()
            locked = await original_lock_run(self, db, budget_run_id)
            if budget_run_id == run_b.id:
                second_run_b_locked.set()
            return locked
        return await original_lock_run(self, db, budget_run_id)

    monkeypatch.setattr(RuntimeBudgetService, "_lock_run", expose_real_cross_run_lock_order)
    reconcile_now = datetime.now(UTC)
    first_task = asyncio.create_task(
        first.reconcile_orphaned_reservations(
            limit=2,
            now=reconcile_now,
            missing_task_grace_seconds=0,
        )
    )
    await asyncio.wait_for(first_run_a_locked.wait(), timeout=5)
    second_task = asyncio.create_task(
        second.reconcile_orphaned_reservations(
            limit=2,
            now=reconcile_now,
            missing_task_grace_seconds=0,
        )
    )
    results = await asyncio.wait_for(
        asyncio.gather(first_task, second_task, return_exceptions=True),
        timeout=10,
    )

    assert all(isinstance(result, int) for result in results), results
    assert sum(results) == 4
    async with owner_sessionmaker() as db:
        settlements = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.reservation_key.in_(tuple(reservation_keys.values())),
                        RuntimeBudgetEvent.event_type == "settlement",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(settlements) == 4
    assert {event.reservation_key for event in settlements} == set(reservation_keys.values())


@pytest.mark.usefixtures("migrated_pg_url")
async def test_orphan_budget_poison_run_is_deferred_without_starving_later_runs(
    owner_sessionmaker,
    budget_artifact_cleanup,
):
    """One malformed run rolls back alone and durably rotates out of the prefix."""

    authority = await _seed_subagent_authority(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    runs = []
    for index in range(3):
        runs.append(
            await service.create_run(
                RuntimeBudgetRunCreate(
                    tenant_id=authority.tenant_id,
                    root_run_kind="web_chat_turn",
                    root_run_key=f"orphan-poison-fairness-{index}-{uuid.uuid4()}",
                    max_tokens=100_000,
                    max_cache_miss_tokens=100_000,
                    max_subagents=4,
                    max_background_tasks=4,
                )
            )
        )
    poison_run, first_good_run, second_good_run = sorted(runs, key=lambda run: run.id)
    budget_artifact_cleanup["run_ids"].update(run.id for run in runs)
    task_ids = {run.id: uuid.uuid4() for run in runs}
    budget_artifact_cleanup["task_ids"].update(task_ids.values())
    reservation_keys = {run.id: f"subagent:{task_ids[run.id].hex}:start" for run in runs}
    for run in runs:
        await service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=run.id,
                reservation_key=reservation_keys[run.id],
                tokens=500,
                cache_miss_tokens=500,
                subagents=1,
                background_tasks=1,
                runtime_task_id=task_ids[run.id],
                reason="subagent_start",
            )
        )

    poison_task_id = task_ids[poison_run.id]
    poison_key = reservation_keys[poison_run.id]
    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=poison_task_id,
                tenant_id=authority.tenant_id,
                task_type="subagent",
                status="completed",
                parent_agent_id=authority.agent_id,
                root_user_id=authority.root_user_id,
                root_session_id=str(authority.parent_session_id),
                delegation_chain_json=[f"agent:{authority.agent_id}", "subagent:explorer"],
                budget_run_id=poison_run.id,
                budget_reservation_key=poison_key,
                completed_at=datetime.now(UTC),
                metadata_json={
                    "budget_settlement_intent": {
                        "schema": "subagent_budget_settlement_intent.v1",
                        "budget_run_id": str(poison_run.id),
                        "reservation_key": poison_key,
                        "runtime_task_id": str(poison_task_id),
                        "reason": "malformed_poison_receipt",
                        "actual_usage": {"tokens": "not-an-int"},
                    }
                },
            )
        )
        await db.commit()

    reconcile_now = datetime.now(UTC) + timedelta(minutes=10)
    first_count = await service.reconcile_orphaned_reservations(
        limit=1,
        now=reconcile_now,
        missing_task_grace_seconds=0,
    )
    second_count = await service.reconcile_orphaned_reservations(
        limit=1,
        now=reconcile_now,
        missing_task_grace_seconds=0,
    )

    async with owner_sessionmaker() as db:
        stored_poison_run = await db.get(RuntimeBudgetRun, poison_run.id)
        settlements = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id.in_(tuple(run.id for run in runs)),
                        RuntimeBudgetEvent.event_type == "settlement",
                    )
                )
            )
            .scalars()
            .all()
        )
    settlement_keys = {event.reservation_key for event in settlements}
    failure = dict((stored_poison_run.metadata_json or {}).get("reservation_reconciliation_failure") or {})

    assert first_count == 1
    assert second_count == 1
    assert poison_key not in settlement_keys
    assert reservation_keys[first_good_run.id] in settlement_keys
    assert reservation_keys[second_good_run.id] in settlement_keys
    assert failure["schema"] == "runtime_budget_reservation_reconciliation_failure.v1"
    assert failure["status"] == "deferred"
    assert failure["attempt_count"] == 1, "the immediate next tick must honor durable deferral"
    assert failure["last_error_type"] == "ValueError"
    assert "not-an-int" in failure["last_error"]
    assert failure["deferred_until"]


@pytest.mark.usefixtures("migrated_pg_url")
async def test_orphan_budget_poison_lifecycle_persists_backoff_history_and_recovers_exactly_once(
    owner_sessionmaker,
    budget_artifact_cleanup,
):
    """A poisoned run retries durably, then recovers without crossing tenant authority."""

    authority = await _seed_subagent_authority(owner_sessionmaker)
    other_authority = await _seed_subagent_authority(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    poison_run = await service.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=authority.tenant_id,
            root_run_kind="web_chat_turn",
            root_run_key=f"orphan-poison-lifecycle-{uuid.uuid4()}",
            max_tokens=100_000,
            max_cache_miss_tokens=100_000,
            max_subagents=4,
            max_background_tasks=4,
        )
    )
    other_run = await service.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=other_authority.tenant_id,
            root_run_kind="web_chat_turn",
            root_run_key=f"orphan-poison-other-tenant-{uuid.uuid4()}",
            max_tokens=100_000,
            max_cache_miss_tokens=100_000,
            max_subagents=4,
            max_background_tasks=4,
        )
    )
    budget_artifact_cleanup["run_ids"].update({poison_run.id, other_run.id})
    poison_task_id = uuid.uuid4()
    other_task_id = uuid.uuid4()
    budget_artifact_cleanup["task_ids"].update({poison_task_id, other_task_id})
    shared_reservation_key = f"subagent:shared-poison-evidence:{uuid.uuid4().hex}"
    for run, task_id in ((poison_run, poison_task_id), (other_run, other_task_id)):
        await service.reserve(
            RuntimeBudgetReservation(
                budget_run_id=run.id,
                reservation_key=shared_reservation_key,
                tokens=500,
                cache_miss_tokens=400,
                subagents=1,
                background_tasks=1,
                runtime_task_id=task_id,
                reason="subagent_start",
            )
        )

    invalid_actual_usage = "not-an-int-first"
    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=poison_task_id,
                tenant_id=authority.tenant_id,
                task_type="subagent",
                status="completed",
                parent_agent_id=authority.agent_id,
                root_user_id=authority.root_user_id,
                root_session_id=str(authority.parent_session_id),
                delegation_chain_json=[f"agent:{authority.agent_id}", "subagent:explorer"],
                budget_run_id=poison_run.id,
                budget_reservation_key=shared_reservation_key,
                completed_at=datetime.now(UTC),
                metadata_json={
                    "budget_settlement_intent": {
                        "schema": "subagent_budget_settlement_intent.v1",
                        "budget_run_id": str(poison_run.id),
                        "reservation_key": shared_reservation_key,
                        "runtime_task_id": str(poison_task_id),
                        "reason": "poison_lifecycle",
                        "actual_usage": {"tokens": invalid_actual_usage},
                    }
                },
            )
        )
        await db.commit()
    async with tenant_scoped_session(other_authority.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=other_task_id,
                tenant_id=other_authority.tenant_id,
                task_type="subagent",
                status="pending",
                parent_agent_id=other_authority.agent_id,
                root_user_id=other_authority.root_user_id,
                root_session_id=str(other_authority.parent_session_id),
                delegation_chain_json=[f"agent:{other_authority.agent_id}", "subagent:explorer"],
                budget_run_id=other_run.id,
                budget_reservation_key=shared_reservation_key,
            )
        )
        await db.commit()

    async def run_snapshot(run_id):
        async with owner_sessionmaker() as db:
            run = await db.get(RuntimeBudgetRun, run_id)
            events = list(
                (
                    await db.execute(
                        select(RuntimeBudgetEvent)
                        .where(RuntimeBudgetEvent.budget_run_id == run_id)
                        .order_by(RuntimeBudgetEvent.created_at, RuntimeBudgetEvent.id)
                    )
                )
                .scalars()
                .all()
            )
        assert run is not None
        dimensions = (
            "tokens",
            "cache_miss_tokens",
            "subagents",
            "team_sessions",
            "delegations",
            "background_tasks",
            "continuation_wakes",
            "provider_calls",
        )
        return {
            "status": run.status,
            "reserved": {
                dimension: int(getattr(run, f"reserved_{dimension}") or 0)
                for dimension in dimensions
                if int(getattr(run, f"reserved_{dimension}") or 0) > 0
            },
            "used": {
                dimension: int(getattr(run, f"used_{dimension}") or 0)
                for dimension in dimensions
                if int(getattr(run, f"used_{dimension}") or 0) > 0
            },
            "metadata": dict(run.metadata_json or {}),
            "events": [
                {
                    "event_type": event.event_type,
                    "reservation_key": event.reservation_key,
                    "amounts": dict(event.amounts_json or {}),
                    "runtime_task_id": str(event.runtime_task_id) if event.runtime_task_id else None,
                    "metadata": dict(event.metadata_json or {}),
                }
                for event in events
            ],
        }

    other_before = await run_snapshot(other_run.id)
    first_now = datetime.now(UTC) + timedelta(minutes=10)
    assert (
        await service.reconcile_orphaned_reservations(
            limit=1,
            now=first_now,
            missing_task_grace_seconds=0,
        )
        == 0
    )
    first_snapshot = await run_snapshot(poison_run.id)
    first_failure = dict(first_snapshot["metadata"]["reservation_reconciliation_failure"])
    assert first_snapshot["reserved"] == {
        "tokens": 500,
        "cache_miss_tokens": 400,
        "subagents": 1,
        "background_tasks": 1,
    }
    assert first_snapshot["used"] == {}
    assert [event for event in first_snapshot["events"] if event["event_type"] == "settlement"] == []
    assert first_failure["status"] == "deferred"
    assert first_failure["attempt_count"] == 1
    assert first_failure["first_error_type"] == "ValueError"
    assert invalid_actual_usage in first_failure["first_error"]
    assert len(first_failure["history"]) == 1

    first_deferred_until = datetime.fromisoformat(first_failure["deferred_until"])
    second_invalid_actual_usage = "not-an-int-second"
    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, poison_task_id, with_for_update=True)
        assert task is not None
        metadata = dict(task.metadata_json or {})
        intent = dict(metadata["budget_settlement_intent"])
        intent["actual_usage"] = {"tokens": second_invalid_actual_usage}
        metadata["budget_settlement_intent"] = intent
        task.metadata_json = metadata
        await db.commit()
    second_now = first_deferred_until + timedelta(seconds=1)
    assert (
        await service.reconcile_orphaned_reservations(
            limit=1,
            now=second_now,
            missing_task_grace_seconds=0,
        )
        == 0
    )
    second_snapshot = await run_snapshot(poison_run.id)
    second_failure = dict(second_snapshot["metadata"]["reservation_reconciliation_failure"])
    second_deferred_until = datetime.fromisoformat(second_failure["deferred_until"])
    assert second_failure["status"] == "deferred"
    assert second_failure["attempt_count"] == 2
    assert second_failure["first_failed_at"] == first_failure["first_failed_at"]
    assert second_failure["first_error_type"] == first_failure["first_error_type"]
    assert second_failure["first_error"] == first_failure["first_error"]
    assert len(second_failure["history"]) == 2
    assert invalid_actual_usage in second_failure["history"][0]["error"]
    assert second_invalid_actual_usage in second_failure["history"][1]["error"]
    assert second_invalid_actual_usage in second_failure["last_error"]
    assert second_deferred_until - second_now > first_deferred_until - first_now
    assert second_snapshot["reserved"] == first_snapshot["reserved"]
    assert second_snapshot["used"] == {}
    assert [event for event in second_snapshot["events"] if event["event_type"] == "settlement"] == []

    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, poison_task_id, with_for_update=True)
        assert task is not None
        metadata = dict(task.metadata_json or {})
        intent = dict(metadata["budget_settlement_intent"])
        intent["actual_usage"] = {"tokens": 321, "cache_miss_tokens": 300, "subagents": 1}
        metadata["budget_settlement_intent"] = intent
        task.metadata_json = metadata
        await db.commit()

    recovery_now = second_deferred_until + timedelta(seconds=1)
    assert (
        await service.reconcile_orphaned_reservations(
            limit=1,
            now=recovery_now,
            missing_task_grace_seconds=0,
        )
        == 1
    )
    assert (
        await service.reconcile_orphaned_reservations(
            limit=1,
            now=recovery_now + timedelta(seconds=1),
            missing_task_grace_seconds=0,
        )
        == 0
    )
    recovered_snapshot = await run_snapshot(poison_run.id)
    recovered_failure = dict(recovered_snapshot["metadata"]["reservation_reconciliation_failure"])
    settlements = [event for event in recovered_snapshot["events"] if event["event_type"] == "settlement"]
    assert len(settlements) == 1
    assert settlements[0]["reservation_key"] == shared_reservation_key
    assert settlements[0]["amounts"] == {
        "tokens": 321,
        "cache_miss_tokens": 300,
        "subagents": 1,
    }
    assert recovered_snapshot["reserved"] == {}
    assert recovered_snapshot["used"] == settlements[0]["amounts"]
    assert recovered_failure["status"] == "recovered"
    assert recovered_failure["attempt_count"] == 2
    assert recovered_failure["first_error_type"] == "ValueError"
    assert invalid_actual_usage in recovered_failure["first_error"]
    assert len(recovered_failure["history"]) == 2
    assert recovered_failure["deferred_until"] is None
    assert recovered_failure["recovered_at"] == recovery_now.isoformat()
    assert await run_snapshot(other_run.id) == other_before


@pytest.mark.usefixtures("migrated_pg_url")
async def test_task_stop_pending_subagent_commits_unified_terminal_protocol(owner_sessionmaker, monkeypatch):
    from app.tools.handlers import command_parity

    await _route_subagent_real_pg(monkeypatch, owner_sessionmaker)
    authority = await _seed_subagent_authority(owner_sessionmaker)
    budget = RuntimeBudgetService(session_factory=owner_sessionmaker)
    budget_run = await budget.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=authority.tenant_id,
            root_run_kind="web_chat_turn",
            root_run_key=f"task-stop-{uuid.uuid4()}",
            max_tokens=100_000,
            max_cache_miss_tokens=100_000,
            max_subagents=4,
            max_background_tasks=4,
        )
    )
    started = await subagent_service.start_subagent_run(
        parent_agent_id=authority.agent_id,
        parent_user_id=authority.root_user_id,
        spec_name="stoppable",
        spec_type="explorer",
        task="wait",
        parent_session_id=str(authority.parent_session_id),
        budget_run_id=budget_run.id,
        budget_service=budget,
    )

    payload = json.loads(
        await command_parity.task_stop(
            _tool_request(
                authority,
                tool_name="task_stop",
                arguments={"runtime_task_id": started.run_id, "reason": "operator stopped pending worker"},
            )
        )
    )
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, uuid.UUID(started.run_id))
        outboxes = list(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(
                        RuntimeNotificationOutbox.source_kind == "subagent",
                        RuntimeNotificationOutbox.source_run_id == str(uuid.UUID(started.run_id)),
                    )
                )
            )
            .scalars()
            .all()
        )

    assert payload["status"] == "killed"
    assert task is not None and task.status == "killed"
    assert task.metadata_json["subagent_decision_entry"]["status"] == "killed"
    assert task.metadata_json["budget_settlement_intent"]["terminal_status"] == "killed"
    assert len(outboxes) == 1 and outboxes[0].terminal_status == "killed"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_task_stop_running_subagent_is_terminalized_by_original_claim(owner_sessionmaker, monkeypatch):
    from app.services.runtime_task_fence import run_claimed_runtime_task
    from app.tools.handlers import command_parity

    await _route_subagent_real_pg(monkeypatch, owner_sessionmaker)
    authority = await _seed_subagent_authority(owner_sessionmaker)
    task_id = uuid.uuid4()
    worker_id = "live-subagent-worker"
    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=authority.tenant_id,
                task_type="subagent",
                status="running",
                parent_agent_id=authority.agent_id,
                parent_session_id=str(authority.parent_session_id),
                child_session_id=str(authority.child_session_id),
                root_user_id=authority.root_user_id,
                root_session_id=str(authority.parent_session_id),
                delegation_chain_json=[f"agent:{authority.agent_id}", "subagent:worker"],
                child_agent_name="worker",
                prompt="work",
                claimed_by=worker_id,
                claim_version=4,
                claim_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
                metadata_json={
                    "subagent_name": "worker",
                    "subagent_type": "explorer",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                },
            )
        )
        await db.commit()

    payload = json.loads(
        await command_parity.task_stop(
            _tool_request(
                authority,
                tool_name="task_stop",
                arguments={"runtime_task_id": str(task_id), "reason": "stop live worker"},
            )
        )
    )
    assert payload["status"] == "cancellation_requested"
    async with owner_sessionmaker() as db:
        requested = await db.get(RuntimeTask, task_id)
        assert requested is not None and requested.status == "running"
        assert requested.metadata_json["cancel_requested"] is True

    await run_claimed_runtime_task(
        subagent_service.dispatch_persisted_subagent_run(task_id.hex),
        task_id=task_id,
        claim_version=4,
        worker_id=worker_id,
        lease_seconds=60,
        session_factory=owner_sessionmaker,
    )
    async with owner_sessionmaker() as db:
        terminal = await db.get(RuntimeTask, task_id)
        outbox = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.source_run_id == str(task_id),
                    RuntimeNotificationOutbox.source_kind == "subagent",
                )
            )
        ).scalar_one()
    assert terminal is not None and terminal.status == "killed"
    assert terminal.metadata_json["subagent_decision_entry"]["status"] == "killed"
    assert outbox.terminal_status == "killed"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_task_stop_pending_claim_race_defers_terminal_to_new_claim_exactly_once(
    owner_sessionmaker,
    monkeypatch,
):
    """A worker claim that wins after stop's read owns the terminal transition."""

    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import run_claimed_runtime_task
    from app.tools.handlers import command_parity

    await _route_subagent_real_pg(monkeypatch, owner_sessionmaker)
    authority = await _seed_subagent_authority(owner_sessionmaker)
    budget = RuntimeBudgetService(session_factory=owner_sessionmaker)
    budget_run = await budget.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=authority.tenant_id,
            root_run_kind="web_chat_turn",
            root_run_key=f"stop-claim-race-{uuid.uuid4()}",
            max_tokens=100_000,
            max_cache_miss_tokens=100_000,
            max_subagents=4,
            max_background_tasks=4,
        )
    )
    task_id = uuid.uuid4()
    reservation_key = f"subagent:{task_id.hex}:start"
    await budget.reserve(
        RuntimeBudgetReservation(
            budget_run_id=budget_run.id,
            reservation_key=reservation_key,
            tokens=500,
            cache_miss_tokens=500,
            subagents=1,
            background_tasks=1,
            runtime_task_id=task_id,
            reason="subagent_start",
        )
    )
    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=authority.tenant_id,
                task_type="subagent",
                status="pending",
                priority=2_000_000_000,
                created_at=datetime(2000, 1, 1, tzinfo=UTC),
                parent_agent_id=authority.agent_id,
                parent_session_id=str(authority.parent_session_id),
                child_session_id=str(authority.child_session_id),
                root_user_id=authority.root_user_id,
                root_session_id=str(authority.parent_session_id),
                delegation_chain_json=[f"agent:{authority.agent_id}", "subagent:worker"],
                prompt="run until cancelled",
                child_agent_name="worker",
                budget_run_id=budget_run.id,
                budget_reservation_key=reservation_key,
                budget_admission_status="reserved",
                metadata_json={
                    "subagent_name": "worker",
                    "subagent_type": "explorer",
                    "execution_backend": "runtime_task_worker",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                    "root_user_id": str(authority.root_user_id),
                    "parent_session_id": str(authority.parent_session_id),
                    "child_session_id": str(authority.child_session_id),
                    "budget_run_id": str(budget_run.id),
                    "budget_reservation_key": reservation_key,
                },
            )
        )
        await db.commit()

    stop_read_complete = asyncio.Event()
    release_stop = asyncio.Event()
    original_get_record = subagent_service.get_runtime_task_record
    service_reads = 0

    async def controlled_get_record(run_id):
        nonlocal service_reads
        record = await original_get_record(run_id)
        service_reads += 1
        if service_reads == 1:
            stop_read_complete.set()
            await release_stop.wait()
        return record

    published = []

    async def capture_publish_subagent_cancel(*, run_id, parent_agent_id):
        published.append((run_id, parent_agent_id))

    monkeypatch.setattr(subagent_service, "get_runtime_task_record", controlled_get_record)
    monkeypatch.setattr("app.services.runtime_control_bus.publish_subagent_cancel", capture_publish_subagent_cancel)
    stop_future = asyncio.create_task(
        command_parity.task_stop(
            _tool_request(
                authority,
                tool_name="task_stop",
                arguments={"runtime_task_id": str(task_id), "reason": "stop during claim"},
            )
        )
    )
    await asyncio.wait_for(stop_read_complete.wait(), timeout=5)
    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        claimed = await RuntimeTaskClaimService(
            db=db,
            worker_id="race-winning-worker",
            task_types=("subagent",),
            lease_seconds=60,
        ).claim_available(batch_size=1)
    assert len(claimed) == 1 and claimed[0].id == task_id
    claim_version = claimed[0].claim_version
    release_stop.set()

    payload = json.loads(await asyncio.wait_for(stop_future, timeout=5))
    assert payload["status"] == "cancellation_requested"
    assert published == [(str(task_id), str(authority.agent_id))]
    async with owner_sessionmaker() as db:
        requested = await db.get(RuntimeTask, task_id)
    assert requested is not None and requested.status == "running"
    assert requested.claimed_by == "race-winning-worker"
    assert requested.claim_version == claim_version
    assert requested.metadata_json["cancel_requested"] is True

    await run_claimed_runtime_task(
        subagent_service.dispatch_persisted_subagent_run(task_id.hex),
        task_id=task_id,
        claim_version=claim_version,
        worker_id="race-winning-worker",
        lease_seconds=60,
        session_factory=owner_sessionmaker,
    )
    assert await subagent_service.dispatch_persisted_subagent_run(task_id.hex) is False
    assert await budget.reconcile_orphaned_reservations() == 0

    async with owner_sessionmaker() as db:
        terminal = await db.get(RuntimeTask, task_id)
        outboxes = list(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(
                        RuntimeNotificationOutbox.source_kind == "subagent",
                        RuntimeNotificationOutbox.source_run_id == str(task_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        settlements = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == budget_run.id,
                        RuntimeBudgetEvent.reservation_key == reservation_key,
                        RuntimeBudgetEvent.event_type == "settlement",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert terminal is not None and terminal.status == "killed"
    assert terminal.metadata_json["subagent_decision_entry"]["status"] == "killed"
    assert terminal.metadata_json["budget_settlement_intent"]["status"] == "pending"
    assert len(outboxes) == 1 and outboxes[0].terminal_status == "killed"
    assert len(settlements) == 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_expired_cancel_request_is_terminalized_by_startup_recovery(owner_sessionmaker, monkeypatch):
    from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks

    await _route_subagent_real_pg(monkeypatch, owner_sessionmaker)
    authority = await _seed_subagent_authority(owner_sessionmaker)
    budget = RuntimeBudgetService(session_factory=owner_sessionmaker)
    budget_run = await budget.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=authority.tenant_id,
            root_run_kind="web_chat_turn",
            root_run_key=f"expired-stop-{uuid.uuid4()}",
            max_tokens=100_000,
            max_cache_miss_tokens=100_000,
            max_subagents=4,
            max_background_tasks=4,
        )
    )
    task_id = uuid.uuid4()
    reservation_key = f"subagent:{task_id.hex}:start"
    await budget.reserve(
        RuntimeBudgetReservation(
            budget_run_id=budget_run.id,
            reservation_key=reservation_key,
            tokens=500,
            cache_miss_tokens=500,
            subagents=1,
            background_tasks=1,
            runtime_task_id=task_id,
            reason="subagent_start",
        )
    )
    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=authority.tenant_id,
                task_type="subagent",
                status="running",
                parent_agent_id=authority.agent_id,
                parent_session_id=str(authority.parent_session_id),
                child_session_id=str(authority.child_session_id),
                root_user_id=authority.root_user_id,
                root_session_id=str(authority.parent_session_id),
                delegation_chain_json=[f"agent:{authority.agent_id}", "subagent:worker"],
                child_agent_name="worker",
                budget_run_id=budget_run.id,
                budget_reservation_key=reservation_key,
                budget_admission_status="reserved",
                claimed_by="dead-worker",
                claim_version=7,
                claim_expires_at=datetime(2020, 1, 1, tzinfo=UTC),
                metadata_json={
                    "subagent_name": "worker",
                    "subagent_type": "worker",
                    "execution_backend": "runtime_task_worker",
                    "budget_run_id": str(budget_run.id),
                    "budget_reservation_key": reservation_key,
                    "cancel_requested": True,
                    "cancel_reason": "stop before restart",
                },
            )
        )
        await db.commit()

    await reconcile_orphaned_runtime_tasks(task_types={"subagent"})
    first_settlement = await budget.reconcile_orphaned_reservations()
    duplicate_settlement = await budget.reconcile_orphaned_reservations()
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, task_id)
        outbox = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.source_run_id == str(task_id),
                    RuntimeNotificationOutbox.source_kind == "subagent",
                )
            )
        ).scalar_one()
        settlements = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == budget_run.id,
                        RuntimeBudgetEvent.reservation_key == reservation_key,
                        RuntimeBudgetEvent.event_type == "settlement",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert task is not None and task.status == "needs_reconciliation"
    assert task.metadata_json["subagent_decision_entry"]["status"] == "needs_reconciliation"
    assert task.metadata_json["budget_settlement_intent"]["runtime_task_id"] == str(task_id)
    assert outbox.terminal_status == "needs_reconciliation"
    assert first_settlement == 1
    assert duplicate_settlement == 0
    assert len(settlements) == 1


@pytest.mark.usefixtures("migrated_pg_url")
async def test_startup_and_claim_quarantine_use_subagent_terminal_protocol(owner_sessionmaker, monkeypatch):
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks

    await _route_subagent_real_pg(monkeypatch, owner_sessionmaker)
    authority = await _seed_subagent_authority(owner_sessionmaker)
    startup_task_id = uuid.uuid4()
    claim_task_id = uuid.uuid4()
    shared = {
        "tenant_id": authority.tenant_id,
        "task_type": "subagent",
        "parent_agent_id": authority.agent_id,
        "parent_session_id": str(authority.parent_session_id),
        "child_session_id": str(authority.child_session_id),
        "root_user_id": authority.root_user_id,
        "root_session_id": str(authority.parent_session_id),
        "delegation_chain_json": [f"agent:{authority.agent_id}", "subagent:worker"],
        "prompt": "resume",
        "child_agent_name": "worker",
    }
    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add_all(
            [
                RuntimeTask(
                    id=startup_task_id,
                    status="running",
                    claimed_by="dead-worker",
                    claim_version=2,
                    claim_expires_at=datetime(2020, 1, 1, tzinfo=UTC),
                    metadata_json={"subagent_name": "worker", "subagent_type": "worker"},
                    **shared,
                ),
                RuntimeTask(
                    id=claim_task_id,
                    status="resumable",
                    claim_version=3,
                    metadata_json={
                        "subagent_name": "worker",
                        "subagent_type": "worker",
                        "reconciliation_operation": {
                            "schema": "runtime_reconciliation_operation.v2",
                            "operation_id": uuid.uuid4().hex,
                            "status": "prepared",
                            "action": "retry",
                        },
                    },
                    **shared,
                ),
            ]
        )
        await db.commit()

    await reconcile_orphaned_runtime_tasks(task_types={"subagent"})
    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        await RuntimeTaskClaimService(
            db=db,
            worker_id="claim-worker",
            task_types=("subagent",),
        ).claim_available(batch_size=100)

    async with owner_sessionmaker() as db:
        tasks = {
            task.id: task
            for task in (
                await db.execute(select(RuntimeTask).where(RuntimeTask.id.in_((startup_task_id, claim_task_id))))
            )
            .scalars()
            .all()
        }
        outboxes = list(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(
                        RuntimeNotificationOutbox.source_kind == "subagent",
                        RuntimeNotificationOutbox.source_run_id.in_((str(startup_task_id), str(claim_task_id))),
                    )
                )
            )
            .scalars()
            .all()
        )

    assert {tasks[startup_task_id].status, tasks[claim_task_id].status} == {"needs_reconciliation"}
    for task in tasks.values():
        assert task.metadata_json["subagent_decision_entry"]["status"] == "needs_reconciliation"
    assert tasks[startup_task_id].metadata_json.get("completion_outbox_id"), tasks[startup_task_id].metadata_json
    assert tasks[claim_task_id].metadata_json.get("completion_outbox_id"), tasks[claim_task_id].metadata_json
    assert {row.source_run_id for row in outboxes} == {str(startup_task_id), str(claim_task_id)}


@pytest.mark.usefixtures("migrated_pg_url")
async def test_startup_subagent_resume_filters_type_before_limit_and_empty_type_scope_is_noop(
    owner_sessionmaker,
    monkeypatch,
):
    from app.services import runtime_task_service, runtime_task_worker

    await _route_subagent_real_pg(monkeypatch, owner_sessionmaker)
    authority = await _seed_subagent_authority(owner_sessionmaker)
    subagent_task_id = uuid.uuid4()
    oldest = datetime.now(UTC) - timedelta(hours=2)
    async with tenant_scoped_session(authority.tenant_id, session_factory=owner_sessionmaker) as db:
        db.add_all(
            [
                RuntimeTask(
                    id=uuid.uuid4(),
                    tenant_id=authority.tenant_id,
                    task_type="workflow",
                    status="pending",
                    parent_agent_id=authority.agent_id,
                    root_user_id=authority.root_user_id,
                    root_session_id=str(authority.parent_session_id),
                    parent_session_id=str(authority.parent_session_id),
                    created_at=oldest + timedelta(seconds=index),
                    metadata_json={"resume_after_restart": True},
                )
                for index in range(50)
            ]
        )
        db.add(
            RuntimeTask(
                id=subagent_task_id,
                tenant_id=authority.tenant_id,
                task_type="subagent",
                status="running",
                parent_agent_id=authority.agent_id,
                parent_session_id=str(authority.parent_session_id),
                child_session_id=str(authority.child_session_id),
                root_user_id=authority.root_user_id,
                root_session_id=str(authority.parent_session_id),
                delegation_chain_json=[f"agent:{authority.agent_id}", "subagent:explorer"],
                child_agent_name="explorer",
                prompt="resume safely",
                claimed_by="expired-worker",
                claim_version=3,
                claim_expires_at=datetime.now(UTC) - timedelta(minutes=5),
                created_at=oldest + timedelta(minutes=10),
                metadata_json={
                    "subagent_name": "explorer",
                    "subagent_type": "explorer",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                    "side_effect_risk": "read_only",
                    "root_user_id": str(authority.root_user_id),
                    "parent_session_id": str(authority.parent_session_id),
                    "child_session_id": str(authority.child_session_id),
                },
            )
        )
        await db.commit()

    assert (
        await runtime_task_service.list_active_runtime_task_records(
            task_types=(),
            limit=50,
            session_factory=owner_sessionmaker,
        )
        == []
    )

    wakes: list[str | None] = []

    async def capture_worker_wake(*, reason, runtime_task_id=None):
        del reason
        wakes.append(runtime_task_id)

    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", capture_worker_wake)
    resumed = await subagent_service.resume_persisted_subagent_runs(limit=50)

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, subagent_task_id)
        outbox_count = int(
            (
                await db.execute(
                    select(func.count(RuntimeNotificationOutbox.id)).where(
                        RuntimeNotificationOutbox.source_kind == "subagent",
                        RuntimeNotificationOutbox.source_run_id == str(subagent_task_id),
                    )
                )
            ).scalar_one()
        )
        await db.execute(
            delete(RuntimeNotificationOutbox).where(
                RuntimeNotificationOutbox.tenant_id == authority.tenant_id,
            )
        )
        await db.commit()

    assert resumed == []
    assert wakes == []
    assert task is not None and task.status == "needs_reconciliation"
    assert task.metadata_json["reconciliation_reason"] == "expired_session_bound_or_mutating_runtime"
    assert outbox_count == 1
