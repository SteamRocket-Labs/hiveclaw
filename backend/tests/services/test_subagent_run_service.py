"""Tests for durable background-subagent run records (Step 8)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select

from app.agents.subagent import SUBAGENT_TYPE_WORKER, SubagentResult, SubagentSpawnContext, SubagentSpec
from app.agents.subagent_memory import SubagentMemoryStore
from app.database import enter_rls_bypass, tenant_scoped_session
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_task import RuntimeTask
from app.services import subagent_run_service as svc
from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
from app.services.runtime_budget_service import RuntimeBudgetApprovalRequired, RuntimeBudgetDenied


async def _delete_subagent_test_outbox(
    session,
    *,
    tenant_id: uuid.UUID,
    source_run_id: str | uuid.UUID | None = None,
) -> None:
    statement = delete(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.tenant_id == tenant_id)
    if source_run_id is not None:
        statement = statement.where(RuntimeNotificationOutbox.source_run_id == str(source_run_id))
    await session.execute(statement)


def test_subagent_outbox_cleanup_never_uses_cross_tenant_delete() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    unscoped_delete = "await session.execute(delete(" + "RuntimeNotificationOutbox))"
    assert unscoped_delete not in source


@pytest.mark.usefixtures("migrated_pg_url")
async def test_subagent_outbox_cleanup_preserves_foreign_tenant_and_run(owner_sessionmaker) -> None:
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User

    owned_tenant_id, foreign_tenant_id = uuid.uuid4(), uuid.uuid4()
    owned_run_id, foreign_run_id = uuid.uuid4(), uuid.uuid4()
    authorities = []
    async with owner_sessionmaker() as session, enter_rls_bypass(session, reason="seed scoped outbox cleanup") as db:
        for label, tenant_id, run_id in (
            ("owned", owned_tenant_id, owned_run_id),
            ("foreign", foreign_tenant_id, foreign_run_id),
        ):
            user_id, agent_id, chat_session_id, outbox_id = (uuid.uuid4() for _ in range(4))
            db.add(Tenant(id=tenant_id, name=f"{label} cleanup", slug=f"{label}-{tenant_id.hex[:8]}"))
            db.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    username=f"{label}-{user_id.hex[:8]}",
                    email=f"{label}-{user_id.hex[:8]}@example.test",
                    password_hash="x",
                    display_name=f"{label} owner",
                )
            )
            await db.flush()
            db.add(
                Agent(
                    id=agent_id,
                    tenant_id=tenant_id,
                    name=f"{label} agent",
                    creator_id=user_id,
                    sponsor_user_id=user_id,
                )
            )
            await db.flush()
            db.add(ChatSession(id=chat_session_id, tenant_id=tenant_id, agent_id=agent_id, user_id=user_id))
            await db.flush()
            db.add(
                RuntimeNotificationOutbox(
                    id=outbox_id,
                    tenant_id=tenant_id,
                    source_kind="subagent",
                    source_run_id=str(run_id),
                    parent_session_id=chat_session_id,
                    parent_agent_id=agent_id,
                    parent_user_id=user_id,
                    terminal_status="completed",
                    task_type="subagent",
                    summary=f"{label} result",
                )
            )
            authorities.append((label, outbox_id))
        await db.commit()

    async with owner_sessionmaker() as session, enter_rls_bypass(session, reason="scoped outbox cleanup") as db:
        await _delete_subagent_test_outbox(
            db,
            tenant_id=owned_tenant_id,
            source_run_id=owned_run_id,
        )
        await db.commit()

    async with owner_sessionmaker() as session, enter_rls_bypass(session, reason="verify scoped outbox cleanup") as db:
        remaining = {
            row.id
            for row in (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(
                        RuntimeNotificationOutbox.id.in_([outbox_id for _, outbox_id in authorities])
                    )
                )
            )
            .scalars()
            .all()
        }
    assert authorities[0][1] not in remaining
    assert authorities[1][1] in remaining
    async with owner_sessionmaker() as session, enter_rls_bypass(session, reason="cleanup scoped outbox test") as db:
        tenant_ids = (owned_tenant_id, foreign_tenant_id)
        await db.execute(delete(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.tenant_id.in_(tenant_ids)))
        await db.execute(delete(ChatSession).where(ChatSession.tenant_id.in_(tenant_ids)))
        await db.execute(delete(Agent).where(Agent.tenant_id.in_(tenant_ids)))
        await db.execute(delete(User).where(User.tenant_id.in_(tenant_ids)))
        await db.execute(delete(Tenant).where(Tenant.id.in_((owned_tenant_id, foreign_tenant_id))))
        await db.commit()


@pytest.mark.asyncio
async def test_start_subagent_run_queues_subagent_task_and_wakes_worker(monkeypatch):
    captured: dict = {}

    async def _fake_create(**kwargs):
        captured.update(kwargs)
        return kwargs["task_id"]

    async def _fake_notify(**kwargs):
        captured["notify"] = kwargs

    async def _fake_child_session(**kwargs):
        captured["child_session"] = kwargs
        return "child-session-1"

    monkeypatch.setattr(svc, "create_runtime_task_record", _fake_create)
    monkeypatch.setattr(svc, "create_subagent_child_session", _fake_child_session)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", _fake_notify)
    parent = uuid.uuid4()
    parent_user = uuid.uuid4()
    parent_session = str(uuid.uuid4())
    root_runtime_task = uuid.uuid4()
    started = await svc.start_subagent_run(
        parent_agent_id=parent,
        parent_user_id=parent_user,
        spec_name="scout",
        spec_type=SUBAGENT_TYPE_WORKER,
        task="do x",
        parent_session_id=parent_session,
        root_runtime_task_id=root_runtime_task,
        context_window_tokens=1_000_000,
    )
    assert started.run_id == captured["task_id"]
    assert started.child_session_id == "child-session-1"
    assert captured["task_type"] == svc.SUBAGENT_RUN_TASK_TYPE == "subagent"
    assert captured["status"] == "pending"
    assert captured["parent_agent_id"] == parent
    assert captured["root_user_id"] == parent_user
    assert captured["root_session_id"] == parent_session
    assert captured["root_runtime_task_id"] == root_runtime_task
    assert captured["delegation_chain"] == [f"agent:{parent}", f"subagent:{started.run_id}:scout"]
    assert captured["child_agent_name"] == "scout"
    assert captured["metadata_json"]["subagent_type"] == SUBAGENT_TYPE_WORKER
    assert captured["metadata_json"]["execution_backend"] == "runtime_task_worker"
    assert captured["metadata_json"]["worker_claim_required"] is True
    assert captured["metadata_json"]["worker_dispatched"] is False
    assert captured["metadata_json"]["resumable_subagent"] is True
    assert captured["metadata_json"]["resume_after_restart"] is True
    assert captured["metadata_json"]["side_effect_risk"] == "mutating"
    assert captured["metadata_json"]["context_window_tokens"] == 1_000_000
    assert captured["metadata_json"]["restart_replay_contract"]["schema"] == "runtime_restart_replay_contract.v1"
    assert (
        captured["metadata_json"]["restart_replay_contract"]["idempotency_key"] == f"subagent:{started.run_id}:restart"
    )
    assert captured["metadata_json"]["restart_replay_journal"][0]["phase"] == "spawn_intent_recorded"
    assert captured["metadata_json"]["restart_replay_journal"][0]["idempotency_key"] == (
        f"subagent:{started.run_id}:restart:spawn_intent_recorded"
    )
    assert "restart_resume_blocker" not in captured["metadata_json"]
    assert captured["notify"] == {"reason": "subagent_created", "runtime_task_id": started.run_id}


@pytest.mark.asyncio
async def test_start_subagent_run_foreground_inline_persists_claimed_running_authority_without_waking_worker(
    monkeypatch,
):
    captured: dict = {}

    async def _fake_create(**kwargs):
        captured["runtime_task"] = kwargs
        return kwargs["task_id"]

    async def _fake_child_session(**kwargs):
        return "child-inline"

    async def _unexpected_notify(**kwargs):
        raise AssertionError(f"foreground inline authority must not wake a second worker: {kwargs}")

    monkeypatch.setattr(svc, "create_runtime_task_record", _fake_create)
    monkeypatch.setattr(svc, "create_subagent_child_session", _fake_child_session)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", _unexpected_notify)

    started = await svc.start_subagent_run(
        parent_agent_id=uuid.uuid4(),
        parent_user_id=uuid.uuid4(),
        spec_name="writer",
        spec_type=SUBAGENT_TYPE_WORKER,
        task="edit the report",
        parent_session_id=str(uuid.uuid4()),
        dispatch_mode="foreground_inline",
    )

    assert started.child_session_id == "child-inline"
    runtime_task = captured["runtime_task"]
    metadata = runtime_task["metadata_json"]
    assert runtime_task["status"] == "running"
    assert runtime_task["claim_version"] == 1
    assert runtime_task["attempt_count"] == 1
    assert runtime_task["claimed_by"] == started.claim_worker_id
    assert runtime_task["claim_expires_at"] == started.claim_expires_at
    assert started.claim_version == 1
    assert started.claim_worker_id.startswith("foreground-subagent:")
    assert started.claim_expires_at > datetime.now(timezone.utc)
    assert metadata["execution_backend"] == "foreground_inline"
    assert metadata["worker_claim_required"] is True
    assert metadata["worker_dispatched"] is True
    assert metadata["claim_version"] == 1
    assert metadata["claimed_by"] == started.claim_worker_id
    assert metadata["claim_expires_at"] == started.claim_expires_at.isoformat()
    assert metadata["claim_fence"] == f"{started.run_id}:1"


def test_subagent_completion_intent_keeps_background_parent_continuation() -> None:
    run_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    notification = svc._subagent_completion_notification(
        record={
            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
            "tenant_id": str(uuid.uuid4()),
            "parent_agent_id": str(uuid.uuid4()),
            "root_user_id": str(uuid.uuid4()),
            "parent_session_id": str(parent_session_id),
            "child_session_id": str(uuid.uuid4()),
            "child_agent_name": "background-worker",
            "metadata": {"execution_backend": "runtime_task_worker"},
        },
        run_id=run_id.hex,
        status="completed",
        summary="background result",
        decision_entry={},
    )

    assert notification is not None
    assert notification.source_run_id == str(run_id)
    assert notification.parent_session_id == parent_session_id
    assert notification.delivery_mode == "parent_continuation"
    assert "local_projection_only" not in notification.metadata


@pytest.mark.asyncio
async def test_start_subagent_run_reserves_runtime_budget_before_creating_task(monkeypatch):
    captured: dict = {}

    async def _fake_create(**kwargs):
        captured["create"] = kwargs
        return kwargs["task_id"]

    class FakeBudgetService:
        async def reserve(self, reservation):
            captured["reservation"] = reservation
            return object()

    monkeypatch.setattr(svc, "create_runtime_task_record", _fake_create)
    parent = uuid.uuid4()
    budget_run_id = uuid.uuid4()

    started = await svc.start_subagent_run(
        parent_agent_id=parent,
        spec_name="scout",
        spec_type=SUBAGENT_TYPE_WORKER,
        task="do x",
        context_window_tokens=1_000_000,
        budget_run_id=budget_run_id,
        budget_service=FakeBudgetService(),
    )

    reservation = captured["reservation"]
    assert reservation.budget_run_id == budget_run_id
    assert reservation.reservation_key == f"subagent:{started.run_id}:start"
    assert reservation.subagents == 1
    assert reservation.background_tasks == 1
    assert reservation.tokens >= 50_000
    assert reservation.cache_miss_tokens == reservation.tokens
    assert captured["create"]["budget_run_id"] == budget_run_id
    assert captured["create"]["budget_reservation_key"] == reservation.reservation_key
    assert captured["create"]["budget_admission_status"] == "reserved"
    assert captured["create"]["metadata_json"]["budget_run_id"] == str(budget_run_id)


@pytest.mark.asyncio
async def test_start_subagent_run_budget_denial_does_not_create_runtime_task(monkeypatch):
    async def _fake_create(**_kwargs):  # pragma: no cover - denied admission must stop before enqueue
        raise AssertionError("RuntimeTask must not be created after budget denial")

    class DenyingBudgetService:
        async def reserve(self, reservation):
            raise RuntimeBudgetDenied("runtime budget exhausted", budget_run_id=reservation.budget_run_id)

    monkeypatch.setattr(svc, "create_runtime_task_record", _fake_create)

    with pytest.raises(RuntimeBudgetDenied):
        await svc.start_subagent_run(
            parent_agent_id=uuid.uuid4(),
            spec_name="scout",
            spec_type=SUBAGENT_TYPE_WORKER,
            task="do x",
            budget_run_id=uuid.uuid4(),
            budget_service=DenyingBudgetService(),
        )


@pytest.mark.asyncio
async def test_start_subagent_run_approval_wait_persists_exact_task_without_waking_worker(monkeypatch):
    captured: dict = {}

    async def _fake_create(**kwargs):
        captured["create"] = kwargs
        return kwargs["task_id"]

    async def _fake_notify(**kwargs):
        captured["notify"] = kwargs

    async def _fake_create_child_session(**kwargs):
        captured["child_session"] = kwargs
        return "waiting-child-session"

    class WaitingBudgetService:
        async def reserve(self, reservation):
            captured["reservation"] = reservation
            raise RuntimeBudgetApprovalRequired(
                "approval required",
                budget_run_id=reservation.budget_run_id,
                dimensions=["subagents"],
            )

    monkeypatch.setattr(svc, "create_runtime_task_record", _fake_create)
    monkeypatch.setattr(svc, "create_subagent_child_session", _fake_create_child_session)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", _fake_notify)

    started = await svc.start_subagent_run(
        parent_agent_id=uuid.uuid4(),
        parent_user_id=uuid.uuid4(),
        spec_name="scout",
        spec_type=SUBAGENT_TYPE_WORKER,
        task="do x",
        budget_run_id=uuid.uuid4(),
        budget_service=WaitingBudgetService(),
    )

    assert started.admission_status == "waiting_budget_approval"
    assert captured["create"]["status"] == "pending"
    assert captured["create"]["budget_admission_status"] == "waiting_budget_approval"
    assert captured["create"]["budget_reservation_key"] == captured["reservation"].reservation_key
    assert captured["child_session"]["session_state"] == "waiting_budget_approval"
    assert "notify" not in captured


@pytest.mark.asyncio
async def test_start_subagent_run_persists_full_spec_snapshot(monkeypatch):
    captured: dict = {}

    async def _fake_create(**kwargs):
        captured.update(kwargs)
        return kwargs["task_id"]

    monkeypatch.setattr(svc, "create_runtime_task_record", _fake_create)
    parent = uuid.uuid4()
    spec = SubagentSpec(
        name="code-reviewer",
        description="Use for code review.",
        type="critic",
        allowed_tools=("read_file", "grep_search"),
        excluded_tools=("write_file",),
        model="inherit",
        max_tool_rounds=7,
        isolation="worktree",
        memory_scope="project",
        system_prompt="Persistent reviewer prompt.",
        background=True,
        permission_mode="acceptEdits",
        skills=("security-review",),
        initial_prompt="Load checklist first.",
        mcp_servers=("github",),
        hooks={"Stop": []},
        color="red",
        effort="high",
    )

    await svc.start_subagent_run(
        parent_agent_id=parent,
        spec_name=spec.name,
        spec_type=spec.type,
        task="review x",
        spec_snapshot=spec,
        definition_name="code-reviewer",
        definition_scope="tenant",
    )

    snapshot = captured["metadata_json"]["subagent_spec"]
    assert snapshot == {
        "name": "code-reviewer",
        "description": "Use for code review.",
        "type": "critic",
        "allowed_tools": ["read_file", "grep_search"],
        "excluded_tools": ["write_file"],
        "model": "inherit",
        "max_tool_rounds": 7,
        "isolation": "worktree",
        "memory_scope": "project",
        "has_own_memory": True,
        "parent_knowledge": "readonly",
        "soul": False,
        "system_prompt": "Persistent reviewer prompt.",
        "disable_tools": False,
        "background": True,
        "permission_mode": "acceptEdits",
        "skills": ["security-review"],
        "initial_prompt": "Load checklist first.",
        "mcp_servers": ["github"],
        "hooks": {"Stop": []},
        "color": "red",
        "effort": "high",
    }
    assert captured["metadata_json"]["definition_name"] == "code-reviewer"
    assert captured["metadata_json"]["definition_scope"] == "tenant"


@pytest.mark.asyncio
async def test_start_subagent_run_creates_child_session_and_records_session_contract(monkeypatch):
    captured: dict = {}

    async def _fake_create_child_session(**kwargs):
        captured["child_session_kwargs"] = kwargs
        return "child-session"

    async def _fake_create(**kwargs):
        captured["runtime_task"] = kwargs
        return kwargs["task_id"]

    monkeypatch.setattr(svc, "create_subagent_child_session", _fake_create_child_session, raising=False)
    monkeypatch.setattr(svc, "create_runtime_task_record", _fake_create)

    parent = uuid.uuid4()
    user = uuid.uuid4()
    started = await svc.start_subagent_run(
        parent_agent_id=parent,
        parent_user_id=user,
        spec_name="scout",
        spec_type="explorer",
        task="read x",
        parent_session_id="parent-session",
        trace_id="trace-1",
        context_mode="none",
    )

    assert started.run_id == captured["runtime_task"]["task_id"]
    assert started.child_session_id == "child-session"
    assert captured["child_session_kwargs"]["parent_agent_id"] == parent
    assert captured["child_session_kwargs"]["parent_user_id"] == user
    assert captured["child_session_kwargs"]["parent_session_id"] == "parent-session"
    assert captured["child_session_kwargs"]["spec_name"] == "scout"
    assert captured["child_session_kwargs"]["spec_type"] == "explorer"
    assert captured["child_session_kwargs"]["run_id"] == started.run_id
    assert captured["runtime_task"]["child_session_id"] == "child-session"
    metadata = captured["runtime_task"]["metadata_json"]
    assert metadata["child_session_id"] == "child-session"
    assert metadata["context_mode"] == "none"
    assert metadata["session_contract"]["kind"] == "subagent_child_session"
    assert metadata["session_contract"]["continuation_address"] == "child-session"
    assert metadata["session_contract"]["run_id"] == started.run_id


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.asyncio
async def test_start_subagent_run_real_pg_creates_child_session_and_runtime_task(owner_sessionmaker, monkeypatch):
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User
    import app.services.runtime_task_service as runtime_task_service

    tenant_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tenant_id, name="subagent-real", slug=f"sa-{tenant_id.hex[:10]}"))
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=user_id,
                username=f"sa-u-{user_id.hex[:10]}",
                email=f"{user_id.hex[:10]}@subagent.test",
                password_hash="x",
                display_name="Subagent Owner",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add(
            Agent(
                id=parent_agent_id,
                tenant_id=tenant_id,
                name="parent-agent",
                role_description="parent",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await session.flush()
        session.add(
            ChatSession(
                id=parent_session_id,
                agent_id=parent_agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
                title="Parent Session",
                source_channel="web",
            )
        )

    def scoped_session(tenant=None, **_kwargs):
        return tenant_scoped_session(tenant, session_factory=owner_sessionmaker)

    async def resolve_tenant(_agent_id, *_args, **_kwargs):
        return tenant_id

    monkeypatch.setattr(svc, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(svc, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(runtime_task_service, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(runtime_task_service, "resolve_tenant_for_agent", resolve_tenant)

    started = await svc.start_subagent_run(
        parent_agent_id=parent_agent_id,
        parent_user_id=user_id,
        spec_name="researcher",
        spec_type="explorer",
        task="inspect the evidence",
        parent_session_id=str(parent_session_id),
        trace_id="trace-skill-fork",
        context_mode="none",
    )

    assert started.child_session_id
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        child_session = (
            await session.execute(select(ChatSession).where(ChatSession.id == uuid.UUID(started.child_session_id)))
        ).scalar_one()
        runtime_task = (
            await session.execute(select(RuntimeTask).where(RuntimeTask.id == uuid.UUID(started.run_id)))
        ).scalar_one()

    assert child_session.source_channel == "subagent"
    assert child_session.session_kind == "subagent"
    assert child_session.runtime_task_id == uuid.UUID(started.run_id)
    assert child_session.parent_session_id == parent_session_id
    assert (
        child_session.transcript_metadata_json["session_contract"]["continuation_address"] == started.child_session_id
    )
    assert child_session.transcript_metadata_json["session_contract"]["run_id"] == started.run_id
    assert runtime_task.task_type == svc.SUBAGENT_RUN_TASK_TYPE
    # Durable enqueue semantics (RTD-32 / budget plane): the run is created
    # pending and a shared worker claims it to running.
    assert runtime_task.status == "pending"
    assert runtime_task.parent_agent_id == parent_agent_id
    assert runtime_task.parent_session_id == str(parent_session_id)
    assert runtime_task.child_session_id == started.child_session_id
    assert runtime_task.metadata_json["session_contract"]["continuation_address"] == started.child_session_id
    assert runtime_task.metadata_json["session_contract"]["run_id"] == started.run_id


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.asyncio
async def test_foreground_subagent_real_pg_claim_is_fenced_and_stale_terminal_cannot_overwrite(
    owner_sessionmaker,
    monkeypatch,
):
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User
    import app.services.runtime_task_service as runtime_task_service

    tenant_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tenant_id, name="subagent-fence", slug=f"saf-{tenant_id.hex[:10]}"))
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=user_id,
                username=f"saf-u-{user_id.hex[:10]}",
                email=f"{user_id.hex[:10]}@subagent-fence.test",
                password_hash="x",
                display_name="Subagent Fence Owner",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add(
            Agent(
                id=parent_agent_id,
                tenant_id=tenant_id,
                name="foreground-parent",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await session.flush()
        session.add(
            ChatSession(
                id=parent_session_id,
                agent_id=parent_agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
                title="Foreground Parent",
                source_channel="web",
            )
        )

    def scoped_session(tenant=None, **_kwargs):
        return tenant_scoped_session(tenant, session_factory=owner_sessionmaker)

    async def resolve_tenant(_agent_id, *_args, **_kwargs):
        return tenant_id

    monkeypatch.setattr(svc, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(svc, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(runtime_task_service, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(runtime_task_service, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(runtime_task_service, "async_session", owner_sessionmaker)

    started = await svc.start_subagent_run(
        parent_agent_id=parent_agent_id,
        parent_user_id=user_id,
        spec_name="writer",
        spec_type="worker",
        task="write once",
        parent_session_id=str(parent_session_id),
        dispatch_mode="foreground_inline",
    )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        row = await session.get(RuntimeTask, uuid.UUID(started.run_id))
        assert row is not None
        assert row.status == "running"
        assert row.claim_version == started.claim_version == 1
        assert row.claimed_by == started.claim_worker_id
        assert row.attempt_count == 1
        assert row.claim_expires_at == started.claim_expires_at
        child_session = await session.get(ChatSession, uuid.UUID(started.child_session_id))
        assert child_session is not None
        assert child_session.runtime_task_id == uuid.UUID(started.run_id)
        assert child_session.transcript_metadata_json["claim_version"] == 1
        assert child_session.transcript_metadata_json["claim_worker_id"] == started.claim_worker_id
        assert child_session.transcript_metadata_json["claim_fence"] == f"{started.run_id}:1"
        row.status = "needs_reconciliation"
        row.claim_version = 2
        row.claimed_by = "runtime-task-worker:reconciler"
        row.claim_expires_at = None
        row.result_summary = "unknown old foreground outcome"

    with pytest.raises(RuntimeError, match="stale.*terminal"):
        await svc.make_run_completer(
            started.run_id,
            expected_claim_version=started.claim_version,
            expected_claim_worker_id=started.claim_worker_id,
        )(
            SubagentResult(
                name="writer",
                type="worker",
                status="completed",
                content="late stale result",
            )
        )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        row = await session.get(RuntimeTask, uuid.UUID(started.run_id))
        assert row is not None
        assert row.status == "needs_reconciliation"
        assert row.claim_version == 2
        assert row.claimed_by == "runtime-task-worker:reconciler"
        assert row.result_summary == "unknown old foreground outcome"


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.asyncio
async def test_foreground_subagent_terminal_intent_repairs_projection_after_crash_exactly_once(
    owner_sessionmaker,
    monkeypatch,
):
    """A terminal foreground result survives a crash before transcript projection.

    The first outbox delivery attempt is also failed after the local projection to
    prove that a duplicate pump does not duplicate child/parent transcript facts.
    The delivery callback is a test double because parent continuation crosses the
    external LLM boundary; the durable DB projection and outbox pump are real.
    """

    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User
    import app.services.runtime_task_service as runtime_task_service

    tenant_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    mismatched_user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()

    async with owner_sessionmaker() as session:
        await _delete_subagent_test_outbox(session, tenant_id=tenant_id)
        await session.commit()

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tenant_id, name="subagent-projection", slug=f"sap-{tenant_id.hex[:10]}"))
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=user_id,
                username=f"sap-u-{user_id.hex[:10]}",
                email=f"{user_id.hex[:10]}@subagent-projection.test",
                password_hash="x",
                display_name="Subagent Projection Owner",
                tenant_id=tenant_id,
            )
        )
        session.add(
            User(
                id=mismatched_user_id,
                username=f"sap-u-{mismatched_user_id.hex[:10]}",
                email=f"{mismatched_user_id.hex[:10]}@subagent-projection.test",
                password_hash="x",
                display_name="Mismatched Projection Owner",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add(
            Agent(
                id=parent_agent_id,
                tenant_id=tenant_id,
                name="projection-parent",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await session.flush()
        session.add(
            ChatSession(
                id=parent_session_id,
                agent_id=parent_agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
                title="Projection Parent",
                source_channel="web",
            )
        )

    def scoped_session(tenant=None, **_kwargs):
        return tenant_scoped_session(tenant, session_factory=owner_sessionmaker)

    async def resolve_tenant(_agent_id, *_args, **_kwargs):
        return tenant_id

    monkeypatch.setattr(svc, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(svc, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(runtime_task_service, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(runtime_task_service, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(runtime_task_service, "async_session", owner_sessionmaker)

    started = await svc.start_subagent_run(
        parent_agent_id=parent_agent_id,
        parent_user_id=user_id,
        spec_name="writer",
        spec_type="worker",
        task="write one durable result",
        parent_session_id=str(parent_session_id),
        dispatch_mode="foreground_inline",
    )
    runtime_task_id = uuid.UUID(started.run_id)
    child_session_id = uuid.UUID(started.child_session_id)

    original_projector = svc.update_subagent_child_session_state_for_run

    async def crash_before_projection(**_kwargs):
        raise RuntimeError("simulated process crash after terminal commit")

    monkeypatch.setattr(svc, "update_subagent_child_session_state_for_run", crash_before_projection)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        await svc.make_run_completer(
            started.run_id,
            expected_claim_version=started.claim_version,
            expected_claim_worker_id=started.claim_worker_id,
        )(
            SubagentResult(
                name="writer",
                type="worker",
                status="completed",
                content="durable foreground result",
                tokens_used=17,
            )
        )
    monkeypatch.setattr(svc, "update_subagent_child_session_state_for_run", original_projector)

    async with owner_sessionmaker() as session:
        task = await session.get(RuntimeTask, runtime_task_id)
        intent = (
            await session.execute(
                select(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.tenant_id == tenant_id,
                    RuntimeNotificationOutbox.source_kind == "subagent",
                    RuntimeNotificationOutbox.source_run_id == str(runtime_task_id),
                )
            )
        ).scalar_one()
        projected_before_pump = list(
            (
                await session.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.run_id == runtime_task_id,
                        ChatTranscriptEvent.event_type.in_(("subagent_task_completed", "child_session")),
                    )
                )
            )
            .scalars()
            .all()
        )

    assert task is not None and task.status == "completed"
    assert task.metadata_json["completion_outbox_id"] == str(intent.id)
    assert intent.status == "pending"
    assert intent.delivery_mode == "session_projection"
    assert intent.metadata_json["local_projection_only"] is True
    assert intent.metadata_json["subagent_terminal_projection_required"] is True
    assert projected_before_pump == []

    repair_authority = {
        "notification_id": intent.id,
        "run_id": str(runtime_task_id),
        "parent_agent_id": parent_agent_id,
        "parent_user_id": user_id,
        "parent_session_id": parent_session_id,
        "child_session_id": child_session_id,
        "status": "completed",
        "summary": "durable foreground result",
    }
    with pytest.raises(RuntimeError, match="authority mismatch"):
        await svc.repair_subagent_terminal_projection_from_notification(
            tenant_id=uuid.uuid4(),
            **repair_authority,
        )
    with pytest.raises(RuntimeError, match="authority mismatch"):
        await svc.repair_subagent_terminal_projection_from_notification(
            tenant_id=tenant_id,
            **{**repair_authority, "parent_user_id": uuid.uuid4()},
        )

    outbox = RuntimeNotificationOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=3,
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        child_session = await session.get(ChatSession, child_session_id)
        assert child_session is not None
        # Lightweight Subagents have no child Agent row. Their child transcript
        # is intentionally owned by the parent Agent, matching the reconciler's
        # target authority predicate.
        assert child_session.agent_id == parent_agent_id
        child_session.user_id = mismatched_user_id

    first = await outbox.drain_once(worker_id="projection-worker-a", item_ids={intent.id})

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        child_session = await session.get(ChatSession, child_session_id)
        assert child_session is not None
        child_session.user_id = user_id

    second = await outbox.drain_once(worker_id="projection-worker-b", item_ids={intent.id})
    duplicate = await outbox.drain_once(worker_id="projection-worker-c", item_ids={intent.id})

    async with owner_sessionmaker() as session:
        stored_intent = await session.get(RuntimeNotificationOutbox, intent.id)
        child_session = await session.get(ChatSession, child_session_id)
        child_events = list(
            (
                await session.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == child_session_id,
                        ChatTranscriptEvent.run_id == runtime_task_id,
                        ChatTranscriptEvent.event_type == "subagent_task_completed",
                    )
                )
            )
            .scalars()
            .all()
        )
        parent_events = list(
            (
                await session.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == parent_session_id,
                        ChatTranscriptEvent.run_id == runtime_task_id,
                        ChatTranscriptEvent.event_type == "child_session",
                    )
                )
            )
            .scalars()
            .all()
        )
        parent_notification_count = (
            await session.execute(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == parent_session_id,
                    ChatTranscriptEvent.run_id == runtime_task_id,
                    ChatTranscriptEvent.event_type == "agent_task_notification",
                )
            )
        ).scalar_one()
        intent_count = (
            await session.execute(
                select(func.count())
                .select_from(RuntimeNotificationOutbox)
                .where(
                    RuntimeNotificationOutbox.tenant_id == tenant_id,
                    RuntimeNotificationOutbox.source_kind == "subagent",
                    RuntimeNotificationOutbox.source_run_id == str(runtime_task_id),
                )
            )
        ).scalar_one()

    assert first["retried"] == 1
    assert second["delivered"] == 1
    assert duplicate["claimed"] == 0
    assert stored_intent is not None and stored_intent.status == "delivered"
    assert stored_intent.delivery_receipt_json["status"] == "local_projection_repaired"
    assert child_session is not None
    assert child_session.transcript_metadata_json["session_state"] == "completed"
    assert child_session.transcript_metadata_json["last_run_id"] == str(runtime_task_id)
    assert len(child_events) == 1
    assert len(parent_events) == 1
    assert parent_notification_count == 0
    assert intent_count == 1

    async with owner_sessionmaker() as session:
        await session.execute(delete(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == intent.id))
        await session.commit()

    repaired = await outbox.reconcile_terminal_tasks_once(limit=1, task_ids={runtime_task_id})
    async with owner_sessionmaker() as session:
        recovered_intent = await session.get(RuntimeNotificationOutbox, intent.id)

    assert repaired == 1
    assert recovered_intent is not None
    assert recovered_intent.delivery_mode == "session_projection"
    assert recovered_intent.metadata_json["local_projection_only"] is True

    replayed = await outbox.drain_once(worker_id="projection-worker-backfill", item_ids={recovered_intent.id})
    async with owner_sessionmaker() as session:
        child_event_count_after_backfill = (
            await session.execute(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == child_session_id,
                    ChatTranscriptEvent.run_id == runtime_task_id,
                    ChatTranscriptEvent.event_type == "subagent_task_completed",
                )
            )
        ).scalar_one()
        parent_event_count_after_backfill = (
            await session.execute(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == parent_session_id,
                    ChatTranscriptEvent.run_id == runtime_task_id,
                    ChatTranscriptEvent.event_type == "child_session",
                )
            )
        ).scalar_one()
        parent_notification_count_after_backfill = (
            await session.execute(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == parent_session_id,
                    ChatTranscriptEvent.run_id == runtime_task_id,
                    ChatTranscriptEvent.event_type == "agent_task_notification",
                )
            )
        ).scalar_one()

    assert replayed["delivered"] == 1
    assert child_event_count_after_backfill == 1
    assert parent_event_count_after_backfill == 1
    assert parent_notification_count_after_backfill == 0


@pytest.mark.parametrize("terminal_status", ["killed", "needs_reconciliation"])
@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.asyncio
async def test_headless_foreground_terminal_paths_commit_local_intent_before_projection_retry(
    owner_sessionmaker,
    monkeypatch,
    terminal_status,
):
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User
    import app.services.runtime_task_service as runtime_task_service

    tenant_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    mismatched_user_id = uuid.uuid4()

    async with owner_sessionmaker() as session:
        await _delete_subagent_test_outbox(session, tenant_id=tenant_id)
        session.add(Tenant(id=tenant_id, name="headless-terminal", slug=f"hlt-{tenant_id.hex[:10]}"))
        session.add_all(
            [
                User(
                    id=user_id,
                    username=f"hlt-u-{user_id.hex[:10]}",
                    email=f"{user_id.hex[:10]}@headless-terminal.test",
                    password_hash="x",
                    display_name="Headless Terminal Owner",
                    tenant_id=tenant_id,
                ),
                User(
                    id=mismatched_user_id,
                    username=f"hlt-u-{mismatched_user_id.hex[:10]}",
                    email=f"{mismatched_user_id.hex[:10]}@headless-terminal.test",
                    password_hash="x",
                    display_name="Mismatched Terminal Owner",
                    tenant_id=tenant_id,
                ),
            ]
        )
        await session.flush()
        session.add(
            Agent(
                id=parent_agent_id,
                tenant_id=tenant_id,
                name="headless-terminal-parent",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await session.commit()

    def scoped_session(tenant=None, **_kwargs):
        return tenant_scoped_session(tenant, session_factory=owner_sessionmaker)

    async def resolve_tenant(_agent_id, *_args, **_kwargs):
        return tenant_id

    monkeypatch.setattr(svc, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(svc, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(runtime_task_service, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(runtime_task_service, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(runtime_task_service, "async_session", owner_sessionmaker)

    started = await svc.start_subagent_run(
        parent_agent_id=parent_agent_id,
        parent_user_id=user_id,
        spec_name="headless-worker",
        spec_type="worker",
        task="finish durably without a parent session",
        parent_session_id=None,
        dispatch_mode="foreground_inline",
    )
    runtime_task_id = uuid.UUID(started.run_id)
    child_session_id = uuid.UUID(started.child_session_id)
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        child_session = await session.get(ChatSession, child_session_id)
        assert child_session is not None
        child_session.user_id = mismatched_user_id

    with pytest.raises(RuntimeError, match="user authority mismatch"):
        if terminal_status == "killed":
            await svc._mark_subagent_run_killed(run_id=started.run_id, summary="cancelled after claim")
        else:
            record = await svc.get_runtime_task_record(started.run_id)
            assert record is not None
            await svc._mark_subagent_run_needs_reconciliation(
                run_id=started.run_id,
                metadata=dict(record["metadata"]),
                blocker="unsafe_pending_tool",
                summary="operator reconciliation required",
                trace_id=None,
                session_id=started.child_session_id,
            )

    async with owner_sessionmaker() as session:
        task = await session.get(RuntimeTask, runtime_task_id)
        intent = (
            await session.execute(
                select(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.tenant_id == tenant_id,
                    RuntimeNotificationOutbox.source_kind == "subagent",
                    RuntimeNotificationOutbox.source_run_id == str(runtime_task_id),
                )
            )
        ).scalar_one()

    assert task is not None and task.status == terminal_status
    assert task.metadata_json["completion_outbox_id"] == str(intent.id)
    assert intent.parent_session_id == child_session_id
    assert intent.child_session_id == child_session_id
    assert intent.delivery_mode == "session_projection"
    assert intent.metadata_json["local_projection_only"] is True
    assert intent.status == "pending"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        child_session = await session.get(ChatSession, child_session_id)
        assert child_session is not None
        child_session.user_id = user_id

    outbox = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker, retry_base_seconds=0)
    first = await outbox.drain_once(worker_id=f"headless-{terminal_status}-a", item_ids={intent.id})
    duplicate = await outbox.drain_once(worker_id=f"headless-{terminal_status}-b", item_ids={intent.id})

    async with owner_sessionmaker() as session:
        stored_intent = await session.get(RuntimeNotificationOutbox, intent.id)
        child_events = list(
            (
                await session.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == child_session_id,
                        ChatTranscriptEvent.run_id == runtime_task_id,
                        ChatTranscriptEvent.event_type == "subagent_task_failed",
                    )
                )
            )
            .scalars()
            .all()
        )
        parent_notifications = (
            await session.execute(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == child_session_id,
                    ChatTranscriptEvent.run_id == runtime_task_id,
                    ChatTranscriptEvent.event_type == "agent_task_notification",
                )
            )
        ).scalar_one()

    assert first["delivered"] == 1
    assert duplicate["claimed"] == 0
    assert stored_intent is not None and stored_intent.status == "delivered"
    assert len(child_events) == 1
    assert parent_notifications == 0

    async with owner_sessionmaker() as session:
        await session.execute(delete(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == intent.id))
        await session.commit()

    repaired = await outbox.reconcile_terminal_tasks_once(limit=1, task_ids={runtime_task_id})
    async with owner_sessionmaker() as session:
        recovered_intent = await session.get(RuntimeNotificationOutbox, intent.id)

    assert repaired == 1
    assert recovered_intent is not None
    assert recovered_intent.parent_session_id == child_session_id
    assert recovered_intent.delivery_mode == "session_projection"
    assert recovered_intent.metadata_json["local_projection_only"] is True

    replayed = await outbox.drain_once(
        worker_id=f"headless-{terminal_status}-backfill",
        item_ids={recovered_intent.id},
    )
    async with owner_sessionmaker() as session:
        child_event_count_after_backfill = (
            await session.execute(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == child_session_id,
                    ChatTranscriptEvent.run_id == runtime_task_id,
                    ChatTranscriptEvent.event_type == "subagent_task_failed",
                )
            )
        ).scalar_one()
        notification_count_after_backfill = (
            await session.execute(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == child_session_id,
                    ChatTranscriptEvent.run_id == runtime_task_id,
                    ChatTranscriptEvent.event_type == "agent_task_notification",
                )
            )
        ).scalar_one()

    assert replayed["delivered"] == 1
    assert child_event_count_after_backfill == 1
    assert notification_count_after_backfill == 0


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.asyncio
async def test_subagent_real_pg_two_workers_reclaim_only_expired_foreground_lease_once(
    owner_sessionmaker,
    monkeypatch,
):
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    import app.services.runtime_task_service as runtime_task_service

    tenant_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    live_id = uuid.uuid4()
    expired_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tenant_id, name="subagent-reclaim", slug=f"sar-{tenant_id.hex[:10]}"))
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=user_id,
                username=f"sar-u-{user_id.hex[:10]}",
                email=f"{user_id.hex[:10]}@subagent-reclaim.test",
                password_hash="x",
                display_name="Subagent Reclaim Owner",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add(Agent(id=parent_agent_id, tenant_id=tenant_id, name="parent", creator_id=user_id))

    def scoped_session(tenant=None, **_kwargs):
        return tenant_scoped_session(tenant, session_factory=owner_sessionmaker)

    async def resolve_tenant(_agent_id, *_args, **_kwargs):
        return tenant_id

    monkeypatch.setattr(runtime_task_service, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(runtime_task_service, "resolve_tenant_for_agent", resolve_tenant)

    common = {
        "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
        "status": "running",
        "parent_agent_id": parent_agent_id,
        "claimed_by": "foreground-subagent:old",
        "claim_version": 1,
        "attempt_count": 1,
    }
    await runtime_task_service.create_runtime_task_record(
        task_id=live_id.hex,
        claim_expires_at=now + timedelta(minutes=5),
        metadata_json={
            "execution_backend": "foreground_inline",
            "resumable_subagent": True,
            "resume_after_restart": True,
        },
        **common,
    )
    await runtime_task_service.create_runtime_task_record(
        task_id=expired_id.hex,
        claim_expires_at=now - timedelta(seconds=1),
        metadata_json={
            "execution_backend": "foreground_inline",
            "resumable_subagent": True,
            "resume_after_restart": True,
            "side_effect_risk": "read_only",
            "restart_replay_contract": {
                "schema": "runtime_restart_replay_contract.v1",
                "idempotency_key": f"subagent:{expired_id.hex}:restart",
                "task_type": "subagent",
                "task_id": expired_id.hex,
                "mode": "durable_restart_replay",
                "requires_completion_journal": True,
            },
        },
        **common,
    )

    async def claim(worker_id: str):
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            return await RuntimeTaskClaimService(
                db=session,
                worker_id=worker_id,
                task_types=(svc.SUBAGENT_RUN_TASK_TYPE,),
                lease_seconds=120,
            ).claim_available(batch_size=10)

    claim_batches = await asyncio.gather(claim("worker:a"), claim("worker:b"))
    claimed_ids = [task.id for batch in claim_batches for task in batch]
    assert claimed_ids.count(expired_id) == 1
    assert live_id not in claimed_ids

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        live = await session.get(RuntimeTask, live_id)
        expired = await session.get(RuntimeTask, expired_id)
        assert live is not None and expired is not None
        assert live.claim_version == 1
        assert live.claimed_by == "foreground-subagent:old"
        assert expired.claim_version == 2
        assert expired.claimed_by in {"worker:a", "worker:b"}
        assert expired.attempt_count == 2


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.asyncio
async def test_kernel_skill_fork_handoff_calls_real_spawn_tool_and_records_child_t0(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _execute_tool_with_hooks
    from app.models.agent import Agent
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.runtime.session import SessionContext
    from app.tools.handlers import subagent as subagent_handler
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
    import app.services.subagent_run_service as subagent_run_service
    import app.services.runtime_task_service as runtime_task_service

    tenant_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    parent_runtime_task_id = uuid.uuid4()
    parent_claim_worker_id = "runtime-task-worker:kernel-skill-fork"

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tenant_id, name="subagent-kernel", slug=f"sak-{tenant_id.hex[:10]}"))
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=user_id,
                username=f"sak-u-{user_id.hex[:10]}",
                email=f"{user_id.hex[:10]}@subagent-kernel.test",
                password_hash="x",
                display_name="Subagent Kernel Owner",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add(
            Agent(
                id=parent_agent_id,
                tenant_id=tenant_id,
                name="parent-agent",
                role_description="parent",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await session.flush()
        session.add(
            ChatSession(
                id=parent_session_id,
                agent_id=parent_agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
                title="Parent Session",
                source_channel="web",
            )
        )
        session.add(
            RuntimeTask(
                id=parent_runtime_task_id,
                tenant_id=tenant_id,
                task_type="web_chat_turn",
                status="running",
                parent_agent_id=parent_agent_id,
                parent_session_id=str(parent_session_id),
                root_user_id=user_id,
                root_session_id=str(parent_session_id),
                root_runtime_task_id=parent_runtime_task_id,
                claimed_by=parent_claim_worker_id,
                claim_version=1,
                claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                attempt_count=1,
                metadata_json={"test_fixture": "kernel_skill_fork_handoff"},
            )
        )

    def scoped_session(tenant=None, **_kwargs):
        return tenant_scoped_session(tenant, session_factory=owner_sessionmaker)

    async def resolve_tenant(_agent_id, *_args, **_kwargs):
        return tenant_id

    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr(svc, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(svc, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(subagent_handler, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(subagent_handler, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(runtime_task_service, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(runtime_task_service, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(subagent_run_service, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(subagent_run_service, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(subagent_handler, "memory_store_for_agent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(subagent_handler, "memory_store_for_tenant", lambda *_args, **_kwargs: None)

    async def no_active_agent_team(_request):
        return None

    monkeypatch.setattr(subagent_handler, "active_agent_team_contract_from_tool_request", no_active_agent_team)

    async def fake_resolve_parent_runtime(_agent_id, **_authority):
        return (
            SimpleNamespace(provider="openai", model="gpt-4.1", api_key="test", base_url=None),
            None,
            SimpleNamespace(id=parent_agent_id, name="parent-agent", tenant_id=tenant_id, creator_id=user_id),
        )

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr(subagent_handler, "_resolve_parent_runtime", fake_resolve_parent_runtime)
    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    session_context = SessionContext(
        session_id=str(parent_session_id),
        metadata={
            "tenant_id": str(tenant_id),
            "agent_id": str(parent_agent_id),
            "runtime_task_id": str(parent_runtime_task_id),
            "claim_version": 1,
            "claim_worker_id": parent_claim_worker_id,
            "pending_skill_handoffs": [
                {
                    "skill": "Research",
                    "skill_slug": "research",
                    "source": "skills/research/SKILL.md",
                    "execution_tool": "spawn_subagent",
                    "tool_arguments": {
                        "prompt": "Use the loaded skill `Research`.",
                        "description": "Skill fork worker for Research",
                        "skill": "Research",
                        "subagent_type": "explorer",
                        "permission_profile": {"mode": "auto", "allowed_tools": ["web_search", "read_file"]},
                    },
                    "permission_profile": {"mode": "auto", "allowed_tools": ["web_search", "read_file"]},
                }
            ],
        },
    )
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "load research"}],
        agent_name="Agent",
        role_description="role",
        agent_id=parent_agent_id,
        user_id=user_id,
        session_context=session_context,
        memory_session_id=str(parent_session_id),
    )

    async def execute_tool(tool_name, args, _request, _emit_event, *, tool_call_id=None):
        if tool_name == "load_skill":
            return "Loaded Research"
        if tool_name == "spawn_subagent":
            context = ToolExecutionContext(
                agent_id=parent_agent_id,
                user_id=user_id,
                tenant_id=str(tenant_id),
                workspace=tmp_path,
                session_id=str(parent_session_id),
                runtime_task_id=str(parent_runtime_task_id),
            )
            return await subagent_handler.spawn_subagent_tool(
                ToolExecutionRequest(tool_name=tool_name, arguments=dict(args), context=context)
            )
        raise AssertionError(f"unexpected tool {tool_name}")

    async def emit_event(_event):
        return None

    result, _effective_args, executed = await _execute_tool_with_hooks(
        execute_tool=execute_tool,
        request=request,
        runtime_config=RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=3),
        tool_name="load_skill",
        tool_args={"name": "Research"},
        tool_call_id="call-load-skill",
        emit_event=emit_event,
    )

    assert executed is True
    handoff_result = json.loads(session_context.metadata["executed_skill_handoffs"][0]["result"])
    child_session_id = uuid.UUID(handoff_result["child_session_id"])
    run_id_text = handoff_result["run_id"]
    run_id = uuid.UUID(run_id_text)
    assert handoff_result["mode"] == "background"
    assert "Skill fork worker `Research` executed through `spawn_subagent`." in result

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        child_session = (
            await session.execute(select(ChatSession).where(ChatSession.id == child_session_id))
        ).scalar_one()
        runtime_task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        start_event = (
            await session.execute(
                select(ChatTranscriptEvent).where(
                    ChatTranscriptEvent.session_id == child_session_id,
                    ChatTranscriptEvent.event_type == "subagent_task_started",
                )
            )
        ).scalar_one()

    assert child_session.source_channel == "subagent"
    assert child_session.parent_session_id == parent_session_id
    assert runtime_task.task_type == svc.SUBAGENT_RUN_TASK_TYPE
    # The skill fork enqueues a durable background run carrying the fork task.
    assert runtime_task.status == "pending"
    assert runtime_task.prompt == "Use the loaded skill `Research`."
    assert runtime_task.child_session_id == str(child_session_id)
    assert start_event.metadata_json["session_contract"]["run_id"] == run_id_text
    assert start_event.content == "Use the loaded skill `Research`."
    assert start_event.projection_status == "pending"
    assert start_event.metadata_json["t0_bridge_pending"] is True


@pytest.mark.asyncio
async def test_record_subagent_child_tool_frame_persists_and_clears_pending_frame(monkeypatch):
    updates: list[tuple[str, dict]] = []

    async def _fake_update(run_id, **kwargs):
        updates.append((run_id, kwargs))
        return True

    monkeypatch.setattr(svc, "update_runtime_task_record", _fake_update)

    await svc.record_subagent_child_tool_frame(
        run_id="run-1",
        tool_name="read_file",
        tool_args={"path": "workspace/a.md"},
        tool_call_id="call-read",
        status="running",
        child_session_id="child-session",
        parent_session_id="parent-session",
        trace_id="trace-1",
    )

    running_metadata = updates[-1][1]["metadata_json"]
    assert running_metadata["child_pending_tool_frame"]["tool_call_id"] == "call-read"
    assert running_metadata["child_pending_tool_frame"]["tool_name"] == "read_file"
    assert running_metadata["child_pending_tool_frame"]["arguments"] == {"path": "workspace/a.md"}
    assert running_metadata["child_pending_tool_frame"]["origin_channel"] == "subagent"
    assert running_metadata["child_pending_tool_frame"]["subagent_run_id"] == "run-1"
    assert running_metadata["child_pending_tool_frames"] == [running_metadata["child_pending_tool_frame"]]

    await svc.record_subagent_child_tool_frame(
        run_id="run-1",
        tool_name="read_file",
        tool_args={"path": "workspace/a.md"},
        tool_call_id="call-read",
        status="done",
        child_session_id="child-session",
        parent_session_id="parent-session",
        trace_id="trace-1",
    )

    done_metadata = updates[-1][1]["metadata_json"]
    assert done_metadata["child_pending_tool_frame"] is None
    assert done_metadata["child_pending_tool_frames"] == []
    assert done_metadata["last_child_tool_frame"]["status"] == "done"
    assert done_metadata["last_child_tool_frame"]["tool_call_id"] == "call-read"
    assert done_metadata["last_child_tool_frame"]["subagent_run_id"] == "run-1"


@pytest.mark.asyncio
async def test_start_subagent_run_marks_readonly_types_restart_resumable(monkeypatch):
    captured: dict = {}

    async def _fake_create(**kwargs):
        captured.update(kwargs)
        return kwargs["task_id"]

    monkeypatch.setattr(svc, "create_runtime_task_record", _fake_create)
    parent = uuid.uuid4()
    await svc.start_subagent_run(parent_agent_id=parent, spec_name="scout", spec_type="explorer", task="read x")

    assert captured["metadata_json"]["subagent_type"] == "explorer"
    assert captured["metadata_json"]["resumable_subagent"] is True
    assert captured["metadata_json"]["resume_after_restart"] is True
    assert captured["metadata_json"]["subagent_name"] == "scout"


@pytest.mark.asyncio
async def test_run_completer_maps_ok_to_completed(monkeypatch):
    captured: dict = {}
    run_id = uuid.uuid4().hex
    tenant_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    parent_user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    child_session_id = uuid.uuid4()

    async def _fake_update(run_id, **fields):
        captured["run_id"] = run_id
        captured.update(fields)
        return True

    async def _fake_session_state(**kwargs):
        captured["session_state_update"] = kwargs

    async def _fake_get_runtime_task_record(_run_id):
        assert _run_id == run_id
        return {
            "task_id": run_id,
            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
            "tenant_id": str(tenant_id),
            "parent_agent_id": str(parent_agent_id),
            "root_user_id": str(parent_user_id),
            "parent_session_id": str(parent_session_id),
            "child_session_id": str(child_session_id),
            "child_agent_name": "scout",
            "metadata": {"execution_backend": "runtime_task_worker"},
        }

    monkeypatch.setattr(svc, "update_runtime_task_record", _fake_update)
    monkeypatch.setattr(svc, "update_subagent_child_session_state_for_run", _fake_session_state)
    monkeypatch.setattr(svc, "get_runtime_task_record", _fake_get_runtime_task_record)
    completer = svc.make_run_completer(run_id)
    await completer(SubagentResult(name="scout", type="worker", status="completed", content="done", tokens_used=42))
    assert captured["run_id"] == run_id
    assert captured["status"] == "completed"
    assert captured["result_summary"] == "done"
    assert captured["token_usage"] == {"total_tokens": 42}
    assert captured["metadata_json"]["completion_journal"][-1]["status"] == "completed"
    assert captured["metadata_json"]["completion_journal"][-1]["idempotency_key"] == f"subagent:{run_id}:completed"
    assert captured["session_state_update"]["run_id"] == str(uuid.UUID(run_id))
    assert captured["session_state_update"]["status"] == "completed"
    assert captured["session_state_update"]["summary"] == "done"


@pytest.mark.asyncio
async def test_run_completer_maps_failure_to_failed(monkeypatch):
    captured: dict = {}

    async def _fake_update(run_id, **fields):
        captured.update(fields)
        return True

    async def _fake_session_state(**_kwargs):
        return None

    monkeypatch.setattr(svc, "update_runtime_task_record", _fake_update)
    monkeypatch.setattr(svc, "update_subagent_child_session_state_for_run", _fake_session_state)
    completer = svc.make_run_completer("run-2")
    await completer(SubagentResult(name="scout", type="worker", status="failed", error="boom"))
    assert captured["status"] == "failed"
    assert "boom" in captured["result_summary"]
    assert captured["metadata_json"]["completion_journal"][-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_run_completer_rejects_stale_foreground_terminal_claim(monkeypatch):
    captured: dict = {}

    async def _stale_update(run_id, **fields):
        captured["run_id"] = run_id
        captured.update(fields)
        return False

    async def _unexpected_session_state(**_kwargs):
        raise AssertionError("stale terminal result must not project to the child session")

    monkeypatch.setattr(svc, "update_runtime_task_record", _stale_update)
    monkeypatch.setattr(svc, "update_subagent_child_session_state_for_run", _unexpected_session_state)

    completer = svc.make_run_completer(
        "a" * 32,
        expected_claim_version=1,
        expected_claim_worker_id="foreground-subagent:old",
    )
    with pytest.raises(RuntimeError, match="stale.*terminal"):
        await completer(SubagentResult(name="writer", type="worker", status="completed", content="late"))

    assert captured["expected_status"] == "running"
    assert captured["expected_claim_version"] == 1
    assert captured["expected_claim_worker_id"] == "foreground-subagent:old"


@pytest.mark.asyncio
async def test_run_completer_rejects_partial_terminal_claim_authority(monkeypatch):
    async def _unexpected_update(*_args, **_kwargs):
        raise AssertionError("partial claim authority must fail before persistence")

    monkeypatch.setattr(svc, "update_runtime_task_record", _unexpected_update)

    with pytest.raises(ValueError, match="claim_version.*claim_worker_id"):
        await svc.make_run_completer(
            "b" * 32,
            expected_claim_version=1,
        )(SubagentResult(name="writer", type="worker", status="completed", content="invalid authority"))


@pytest.mark.asyncio
async def test_subagent_completion_projects_child_session_event_to_parent(monkeypatch):
    child_session_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    parent_user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    captured_events: list[dict] = []

    session = SimpleNamespace(
        id=child_session_id,
        agent_id=parent_agent_id,
        tenant_id=tenant_id,
        user_id=parent_user_id,
        transcript_metadata_json={"session_contract": {"kind": "subagent_child_session"}},
        root_session_id=parent_session_id,
        parent_session_id=parent_session_id,
        visibility_scope="team",
        listed_surface="parent",
    )

    class _Scalar:
        def scalar_one_or_none(self):
            return session

    class _FakeSession:
        async def execute(self, _stmt):
            return _Scalar()

        async def commit(self):
            return None

    class _Ctx:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, *_exc):
            return False

    async def fake_get_runtime_task_record(_run_id):
        return {
            "task_id": "run-1",
            "parent_agent_id": str(parent_agent_id),
            "child_session_id": str(child_session_id),
            "parent_session_id": str(parent_session_id),
            "metadata": {
                "child_session_id": str(child_session_id),
                "subagent_name": "critic",
                "subagent_type": "critic",
                "restart_resume_mode": "transcript",
            },
        }

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    async def fake_append_session_event(**kwargs):
        captured_events.append(kwargs)
        return SimpleNamespace(event_id=uuid.uuid4(), sequence=len(captured_events), message_id=None)

    captured_wakeups: list[dict] = []

    async def fake_wake_parent_session_from_subagent_completion(**kwargs):
        captured_wakeups.append(kwargs)

    monkeypatch.setattr(svc, "get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr(svc, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(svc, "tenant_scoped_session", lambda _tenant_id: _Ctx())
    monkeypatch.setattr(svc, "append_session_event", fake_append_session_event)
    monkeypatch.setattr(
        svc,
        "_wake_parent_session_from_subagent_completion",
        fake_wake_parent_session_from_subagent_completion,
        raising=False,
    )

    await svc.update_subagent_child_session_state_for_run(run_id="run-1", status="completed", summary="done")

    parent_event = next(event for event in captured_events if event["session_id"] == parent_session_id)
    assert parent_event["event_type"] == "child_session"
    assert parent_event["role"] == "system"
    assert parent_event["metadata"]["subagent_decision_entry"]["schema"] == "hive.ccplus.subagent_decision_entry.v1"
    assert parent_event["metadata"]["subagent_decision_entry"]["status"] == "completed"
    assert parent_event["metadata"]["subagent_decision_entry"]["safe_to_retry"] is True
    assert parent_event["parts"] == [
        {
            "type": "event",
            "event_type": "child_session",
            "title": "Child Session",
            "text": "done",
            "status": "completed",
            "runtime_task_id": "run-1",
            "child_session_id": str(child_session_id),
            "parent_session_id": str(parent_session_id),
            "root_session_id": str(parent_session_id),
            "reason": "subagent_task_completed",
        }
    ]
    assert captured_wakeups == [
        {
            "db": captured_wakeups[0]["db"],
            "run_id": "run-1",
            "tenant_id": tenant_id,
            "parent_agent_id": parent_agent_id,
            "parent_user_id": parent_user_id,
            "parent_session_id": parent_session_id,
            "child_session_id": child_session_id,
            "status": "completed",
            "summary": "done",
            "subagent_decision_entry": parent_event["metadata"]["subagent_decision_entry"],
        }
    ]


@pytest.mark.asyncio
async def test_subagent_parent_completion_uses_durable_outbox(monkeypatch):
    captured: dict = {}
    db = object()

    async def fake_enqueue(actual_db, notification):
        captured["db"] = actual_db
        captured["notification"] = notification
        return uuid.uuid4()

    monkeypatch.setattr(svc, "enqueue_completion_notification", fake_enqueue, raising=False)
    tenant_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    parent_user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    child_session_id = uuid.uuid4()

    await svc._wake_parent_session_from_subagent_completion(
        db=db,
        run_id="run-1",
        tenant_id=tenant_id,
        parent_agent_id=parent_agent_id,
        parent_user_id=parent_user_id,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        status="completed",
        summary="done",
        subagent_decision_entry={"schema": "decision.v1"},
    )

    notification = captured["notification"]
    assert captured["db"] is db
    assert notification.source_kind == "subagent"
    assert notification.source_run_id == "run-1"
    assert notification.parent_session_id == parent_session_id
    assert notification.child_session_id == child_session_id
    assert notification.delivery_mode == "parent_continuation"
    assert notification.metadata["subagent_decision_entry"] == {"schema": "decision.v1"}


@pytest.mark.asyncio
async def test_get_subagent_run_is_ownership_scoped(monkeypatch):
    from app.core.execution_context import ExecutionPrincipal

    owner = uuid.uuid4()
    other = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()

    async def _fake_get(_run_id):
        return {
            "task_type": "subagent",
            "tenant_id": str(tenant_id),
            "parent_agent_id": str(owner),
            "root_user_id": str(user_id),
            "root_session_id": str(session_id),
            "delegation_chain": [f"agent:{owner}", "subagent:rid:scout"],
            "status": "completed",
            "result": "r",
        }

    monkeypatch.setattr(svc, "get_runtime_task_record", _fake_get)
    owner_principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=owner,
        requester_user_id=user_id,
        root_session_id=str(session_id),
    )
    other_principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=other,
        requester_user_id=user_id,
        root_session_id=str(session_id),
    )
    assert await svc.get_subagent_run("rid", owner, principal=owner_principal) is not None
    assert await svc.get_subagent_run("rid", other, principal=other_principal) is None


@pytest.mark.asyncio
async def test_get_subagent_run_rejects_non_subagent_task(monkeypatch):
    from app.core.execution_context import ExecutionPrincipal

    owner = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()

    async def _fake_get(_run_id):
        return {"task_type": "web_chat_turn", "parent_agent_id": str(owner), "status": "running"}

    monkeypatch.setattr(svc, "get_runtime_task_record", _fake_get)
    assert (
        await svc.get_subagent_run(
            "rid",
            owner,
            principal=ExecutionPrincipal(
                tenant_id=tenant_id,
                source_agent_id=owner,
                requester_user_id=user_id,
                root_session_id=str(session_id),
            ),
        )
        is None
    )


def test_spawn_schema_exposes_run_in_background_and_check_tool_registered():
    from app.tools.handlers.subagent import _SPAWN_PARAMETERS, check_subagent, spawn_subagent_tool  # noqa: F401

    assert "run_in_background" in _SPAWN_PARAMETERS["properties"]
    assert "prompt" in _SPAWN_PARAMETERS["properties"]
    assert "subagent_type" in _SPAWN_PARAMETERS["properties"]
    assert "enum" not in _SPAWN_PARAMETERS["properties"]["subagent_type"]
    assert "general-purpose" in _SPAWN_PARAMETERS["properties"]["subagent_type"]["description"]
    # check_subagent is a registered @tool (callable handler).
    assert callable(check_subagent)


def test_subagent_task_type_uses_metadata_resumability():
    # Restart-resumable subagent records are preserved for the restart pump; old
    # records without explicit resumability still fail closed.
    from app.services.runtime_task_service import _is_restart_resumable_runtime_task

    resumable = type(
        "RuntimeTaskStub",
        (),
        {
            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
            "metadata_json": {"resume_after_restart": True, "resumable_subagent": True},
        },
    )()
    unsafe = type(
        "RuntimeTaskStub",
        (),
        {
            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
            "metadata_json": {"resume_after_restart": False, "resumable_subagent": False},
        },
    )()

    assert _is_restart_resumable_runtime_task(resumable) is True
    assert _is_restart_resumable_runtime_task(unsafe) is False


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_rehydrates_readonly_worker(monkeypatch):
    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    calls: dict[str, object] = {}

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running"), task_types=None):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "status": "pending",
                "parent_agent_id": str(parent),
                "child_agent_name": "scout",
                "prompt": "read x",
                "trace_id": "trace-subagent",
                "parent_session_id": "parent-session",
                "metadata": {
                    "subagent_type": "explorer",
                    "subagent_name": "scout",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                },
            }
        ]

    async def fake_resolve_parent_runtime(_parent_agent_id, **_authority):
        raise AssertionError("startup resume must enqueue; worker dispatch resolves runtime")

    async def fake_spawn_subagent(*_args, **_kwargs):
        raise AssertionError("startup resume must not spawn in the startup process")

    async def fake_update_runtime_task_record(task_id, **kwargs):
        calls.setdefault("updates", []).append((task_id, kwargs))
        return True

    async def fake_notify(**kwargs):
        calls["notify"] = kwargs

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "requeue_runtime_task_for_worker", fake_update_runtime_task_record)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", fake_notify)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == [run_id]
    assert calls["updates"][-1][0] == run_id
    assert calls["updates"][-1][1]["task_type"] == svc.SUBAGENT_RUN_TASK_TYPE
    assert calls["updates"][-1][1]["metadata_json"]["resumed_after_restart"] is True
    assert calls["updates"][-1][1]["metadata_json"]["worker_dispatched"] is False
    assert calls["notify"] == {"reason": "subagent_resumed_after_restart", "runtime_task_id": run_id}


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_skips_live_claim_and_routes_expired_claim_to_shared_worker(monkeypatch):
    live_run_id = uuid.uuid4().hex
    expired_run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    now = datetime.now(timezone.utc)
    notifications: list[dict] = []

    async def fake_list_active_runtime_task_records(**_kwargs):
        def record(run_id, claim_expires_at):
            return {
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "status": "running",
                "parent_agent_id": str(parent),
                "prompt": "resume safely",
                "claim_version": 1,
                "claimed_by": "foreground-subagent:old",
                "metadata": {
                    "subagent_type": "explorer",
                    "subagent_name": "scout",
                    "execution_backend": "foreground_inline",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                    "side_effect_risk": "read_only",
                    "restart_replay_contract": {
                        "schema": "runtime_restart_replay_contract.v1",
                        "idempotency_key": f"subagent:{run_id}:restart",
                        "task_type": "subagent",
                        "task_id": run_id,
                        "mode": "durable_restart_replay",
                        "requires_completion_journal": True,
                    },
                },
                "task_id": run_id,
                "claim_expires_at": claim_expires_at,
            }

        return [
            record(live_run_id, (now + timedelta(minutes=2)).isoformat()),
            record(expired_run_id, (now - timedelta(seconds=1)).isoformat()),
        ]

    async def unexpected_update(*_args, **_kwargs):
        raise AssertionError("startup must not rewrite a running lease into resumable")

    async def fake_notify(**kwargs):
        notifications.append(kwargs)

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "update_runtime_task_record", unexpected_update)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", fake_notify)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == [expired_run_id]
    assert notifications == [{"reason": "subagent_expired_claim_ready", "runtime_task_id": expired_run_id}]


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_uses_full_spec_snapshot(monkeypatch):
    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    calls: dict[str, object] = {}

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running"), task_types=None):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "status": "pending",
                "parent_agent_id": str(parent),
                "child_agent_name": "code-reviewer",
                "prompt": "review x",
                "trace_id": "trace-subagent",
                "parent_session_id": "parent-session",
                "metadata": {
                    "subagent_type": "critic",
                    "subagent_name": "code-reviewer",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                    "subagent_spec": {
                        "name": "code-reviewer",
                        "description": "Use for code review.",
                        "type": "critic",
                        "allowed_tools": ["read_file", "grep_search"],
                        "excluded_tools": ["write_file"],
                        "model": "inherit",
                        "max_tool_rounds": 7,
                        "isolation": "worktree",
                        "memory_scope": "project",
                        "system_prompt": "Persistent reviewer prompt.",
                        "background": True,
                        "permission_mode": "acceptEdits",
                        "skills": ["security-review"],
                        "initial_prompt": "Load checklist first.",
                        "mcp_servers": ["github"],
                        "hooks": {"Stop": []},
                        "color": "red",
                        "effort": "high",
                    },
                },
            }
        ]

    async def fake_resolve_parent_runtime(_parent_agent_id, **_authority):
        raise AssertionError("startup resume must enqueue; worker dispatch resolves runtime")

    async def fake_spawn_subagent(*_args, **_kwargs):
        raise AssertionError("startup resume must not spawn in the startup process")

    async def fake_update_runtime_task_record(task_id, **kwargs):
        calls.setdefault("updates", []).append((task_id, kwargs))
        return True

    async def fake_notify(**kwargs):
        calls["notify"] = kwargs

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "requeue_runtime_task_for_worker", fake_update_runtime_task_record)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", fake_notify)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == [run_id]
    assert calls["updates"][-1][1]["task_type"] == svc.SUBAGENT_RUN_TASK_TYPE
    resumed_snapshot = calls["updates"][-1][1]["metadata_json"]["subagent_spec"]
    assert resumed_snapshot["name"] == "code-reviewer"
    assert resumed_snapshot["allowed_tools"] == ["read_file", "grep_search"]
    assert resumed_snapshot["excluded_tools"] == ["write_file"]
    assert resumed_snapshot["isolation"] == "worktree"
    assert resumed_snapshot["permission_mode"] == "acceptEdits"
    assert resumed_snapshot["skills"] == ["security-review"]
    assert resumed_snapshot["mcp_servers"] == ["github"]
    assert resumed_snapshot["hooks"] == {"Stop": []}
    assert calls["notify"]["runtime_task_id"] == run_id


@pytest.mark.asyncio
async def test_dispatch_persisted_subagent_run_uses_full_spec_snapshot(monkeypatch):
    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    tenant_id = uuid.uuid4()
    parent_user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    child_session_id = str(uuid.uuid4())
    calls: dict[str, object] = {}

    async def fake_get_runtime_task_record(task_id):
        assert task_id == run_id
        return {
            "task_id": run_id,
            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
            "status": "running",
            "claim_version": 4,
            "claimed_by": "runtime-task-worker:test",
            "claim_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
            "tenant_id": str(tenant_id),
            "parent_agent_id": str(parent),
            "root_user_id": str(parent_user_id),
            "child_agent_name": "code-reviewer",
            "prompt": "review x",
            "trace_id": "trace-subagent",
            "parent_session_id": str(parent_session_id),
            "child_session_id": child_session_id,
            "metadata": {
                "subagent_type": "critic",
                "subagent_name": "code-reviewer",
                "resume_after_restart": True,
                "resumable_subagent": True,
                "subagent_spec": {
                    "name": "code-reviewer",
                    "description": "Use for code review.",
                    "type": "critic",
                    "allowed_tools": ["read_file", "grep_search"],
                    "excluded_tools": ["write_file"],
                    "model": "inherit",
                    "max_tool_rounds": 7,
                    "isolation": "worktree",
                    "memory_scope": "project",
                    "system_prompt": "Persistent reviewer prompt.",
                    "background": True,
                    "permission_mode": "acceptEdits",
                    "skills": ["security-review"],
                    "initial_prompt": "Load checklist first.",
                    "mcp_servers": ["github"],
                    "hooks": {"Stop": []},
                    "color": "red",
                    "effort": "high",
                },
            },
        }

    async def fake_resolve_parent_runtime(parent_agent_id, **_authority):
        calls["resolved_parent"] = parent_agent_id
        return {
            "ctx_kwargs": {
                "parent_agent_id": parent,
                "parent_user_id": parent_user_id,
                "model": object(),
                "parent_agent_name": "Parent",
                "tenant_id": tenant_id,
            }
        }

    async def fake_spawn_subagent(ctx, spec, task, **kwargs):
        calls["ctx"] = ctx
        calls["spec"] = spec
        calls["task"] = task
        calls["kwargs"] = kwargs
        return SimpleNamespace(
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="review done")
        )

    async def fake_update_runtime_task_record(task_id, **kwargs):
        calls.setdefault("updates", []).append((task_id, kwargs))
        return True

    async def fake_update_child_session(**kwargs):
        calls["child_session_update"] = kwargs

    monkeypatch.setattr(svc, "get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(svc, "update_subagent_child_session_state_for_run", fake_update_child_session)

    dispatched = await svc.dispatch_persisted_subagent_run(run_id)

    assert dispatched is True
    assert calls["resolved_parent"] == parent
    assert calls["ctx"].subagent_run_id == run_id
    assert calls["ctx"].child_session_id == child_session_id
    assert calls["ctx"].recovery_metadata["runtime_task_id"] == run_id
    assert calls["ctx"].recovery_metadata["claim_version"] == 4
    assert calls["ctx"].recovery_metadata["claim_worker_id"] == "runtime-task-worker:test"
    spec = calls["spec"]
    assert spec.name == "code-reviewer"
    assert spec.description == "Use for code review."
    assert spec.type == "critic"
    assert spec.allowed_tools == ("read_file", "grep_search")
    assert spec.excluded_tools == ("write_file",)
    assert spec.isolation == "worktree"
    assert spec.permission_mode == "acceptEdits"
    assert spec.skills == ("security-review",)
    assert spec.mcp_servers == ("github",)
    assert spec.hooks == {"Stop": []}
    assert calls["task"] == "review x"
    assert calls["kwargs"]["run_in_background"] is False
    terminal_update = calls["updates"][-1][1]
    assert terminal_update["expected_status"] == "running"
    assert terminal_update["expected_claim_version"] == 4
    assert terminal_update["expected_claim_worker_id"] == "runtime-task-worker:test"
    assert calls["kwargs"]["fork"] == "worktree"
    assert calls["updates"][0][1]["metadata_json"]["worker_dispatched"] is True
    assert calls["updates"][-1][1]["status"] == "completed"
    assert calls["child_session_update"]["status"] == "completed"


@pytest.mark.asyncio
async def test_dispatch_persisted_subagent_run_stops_before_spawn_when_claim_cas_is_stale(monkeypatch):
    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    tenant_id = uuid.uuid4()
    parent_user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    child_session_id = uuid.uuid4()

    async def fake_get_runtime_task_record(_task_id):
        return {
            "task_id": run_id,
            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
            "status": "running",
            "claim_version": 3,
            "claimed_by": "runtime-task-worker:old",
            "claim_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
            "tenant_id": str(tenant_id),
            "parent_agent_id": str(parent),
            "root_user_id": str(parent_user_id),
            "parent_session_id": str(parent_session_id),
            "child_session_id": str(child_session_id),
            "prompt": "read safely",
            "metadata": {
                "subagent_type": "explorer",
                "subagent_name": "scout",
                "resume_after_restart": True,
                "resumable_subagent": True,
            },
        }

    async def fake_load_resume_messages(**_kwargs):
        return [{"role": "user", "content": "resume"}]

    async def fake_resolve_parent_runtime(_parent_agent_id, **_authority):
        return SubagentSpawnContext(
            parent_agent_id=parent,
            parent_user_id=parent_user_id,
            model=object(),
            tenant_id=tenant_id,
        )

    async def fake_hydrate(runtime, **_kwargs):
        return runtime

    async def stale_update(*_args, **_kwargs):
        return False

    async def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError("stale claimed worker must stop before subagent spawn")

    monkeypatch.setattr(svc, "get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr(svc, "_load_subagent_resume_messages", fake_load_resume_messages)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime)
    monkeypatch.setattr(svc, "_hydrate_worker_runtime_context", fake_hydrate)
    monkeypatch.setattr(svc, "update_runtime_task_record", stale_update)
    monkeypatch.setattr(svc, "spawn_subagent", unexpected_spawn)

    with pytest.raises(RuntimeError, match="stale.*before dispatch"):
        await svc.dispatch_persisted_subagent_run(run_id)


@pytest.mark.asyncio
async def test_dispatch_general_purpose_with_child_transcript_uses_resume_messages(monkeypatch):
    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    tenant_id = uuid.uuid4()
    parent_user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    child_session_id = str(uuid.uuid4())
    resume_messages = [
        {"role": "user", "content": "original task"},
        {"role": "assistant", "content": "partial progress"},
        {"role": "user", "content": "resume the interrupted task"},
    ]
    calls: dict[str, object] = {}

    async def fake_get_runtime_task_record(task_id):
        assert task_id == run_id
        return {
            "task_id": run_id,
            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
            "status": "running",
            "tenant_id": str(tenant_id),
            "parent_agent_id": str(parent),
            "root_user_id": str(parent_user_id),
            "child_agent_name": "general-purpose",
            "prompt": "resume the interrupted task",
            "trace_id": "trace-subagent",
            "parent_session_id": str(parent_session_id),
            "child_session_id": child_session_id,
            "metadata": {
                "subagent_type": "general-purpose",
                "subagent_name": "general-purpose",
                "resume_after_restart": True,
                "resumable_subagent": True,
                "child_session_id": child_session_id,
                "context_window_tokens": 1_000_000,
            },
        }

    async def fake_load_subagent_resume_messages(**kwargs):
        calls["resume_kwargs"] = kwargs
        return list(resume_messages)

    async def fake_resolve_parent_runtime(parent_agent_id, **_authority):
        return {
            "ctx_kwargs": {
                "parent_agent_id": parent_agent_id,
                "parent_user_id": parent_user_id,
                "model": object(),
                "parent_agent_name": "Parent",
                "tenant_id": tenant_id,
            }
        }

    async def fake_spawn_subagent(ctx, spec, task, **kwargs):
        calls["ctx"] = ctx
        calls["spec"] = spec
        calls["task"] = task
        calls["kwargs"] = kwargs
        return SimpleNamespace(
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="resumed")
        )

    async def fake_update_runtime_task_record(task_id, **kwargs):
        calls.setdefault("updates", []).append((task_id, kwargs))
        return True

    async def fake_update_child_session(**kwargs):
        calls["child_session_update"] = kwargs

    monkeypatch.setattr(svc, "get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr(svc, "_load_subagent_resume_messages", fake_load_subagent_resume_messages, raising=False)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(svc, "update_subagent_child_session_state_for_run", fake_update_child_session)

    dispatched = await svc.dispatch_persisted_subagent_run(run_id)

    assert dispatched is True
    assert calls["resume_kwargs"]["child_session_id"] == child_session_id
    assert calls["resume_kwargs"]["context_window_tokens"] == 1_000_000
    assert calls["spec"].type == "general-purpose"
    assert calls["kwargs"]["resume_messages"] == resume_messages
    assert calls["task"] == "resume the interrupted task"
    assert calls["updates"][0][1]["status"] == "running"
    assert calls["updates"][-1][1]["status"] == "completed"


@pytest.mark.asyncio
async def test_load_subagent_resume_messages_uses_budget_not_fixed_event_count(monkeypatch):
    parent_agent_id = uuid.uuid4()
    child_session_id = uuid.uuid4().hex

    events = []
    for i in range(450):
        events.append(
            SimpleNamespace(
                event_type="user_message",
                role="user",
                content=f"event-{i}",
                metadata={},
            )
        )

    def fake_replay_t0_session_events(**kwargs):
        assert kwargs["agent_id"] == parent_agent_id
        assert kwargs["session_id"] == child_session_id
        return list(events)

    monkeypatch.setattr("app.memory.t0.ledger.replay_t0_session_events", fake_replay_t0_session_events)

    messages = await svc._load_subagent_resume_messages(
        parent_agent_id=parent_agent_id,
        child_session_id=child_session_id,
        prompt="continue",
        max_resume_chars=100_000,
    )

    assert messages[0]["content"] == "event-0"
    assert any(message["content"] == "event-449" for message in messages)
    assert messages[-1] == {"role": "user", "content": "continue"}


@pytest.mark.asyncio
async def test_load_subagent_resume_messages_preserves_tool_metadata_without_executable_tool_calls(monkeypatch):
    parent_agent_id = uuid.uuid4()
    child_session_id = uuid.uuid4().hex
    events = [
        SimpleNamespace(event_type="user_message", role="user", content="start", metadata={}),
        SimpleNamespace(
            event_type="tool_call",
            role="tool",
            content='{"path":"workspace/a.md"}',
            metadata={
                "tool_call_id": "call-1",
                "tool_name": "read_file",
                "arguments": {"path": "workspace/a.md"},
                "status": "started",
            },
        ),
        SimpleNamespace(
            event_type="tool_result",
            role="tool",
            content="file contents",
            metadata={
                "tool_call_id": "call-1",
                "tool_name": "read_file",
                "status": "completed",
            },
        ),
        SimpleNamespace(event_type="assistant_message", role="assistant", content="done", metadata={}),
    ]

    monkeypatch.setattr("app.memory.t0.ledger.replay_t0_session_events", lambda **_kwargs: list(events))

    messages = await svc._load_subagent_resume_messages(
        parent_agent_id=parent_agent_id,
        child_session_id=child_session_id,
        prompt="continue",
        max_resume_chars=100_000,
    )

    tool_messages = [message for message in messages if "<subagent-transcript-event" in message["content"]]
    assert tool_messages
    assert all(message["role"] == "user" for message in tool_messages)
    assert all("tool_calls" not in message for message in tool_messages)
    assert '"tool_call_id": "call-1"' in tool_messages[0]["content"]
    assert '"tool_name": "read_file"' in tool_messages[0]["content"]
    assert '"arguments": {"path": "workspace/a.md"}' in tool_messages[0]["content"]
    assert '"status": "completed"' in tool_messages[1]["content"]


@pytest.mark.asyncio
async def test_dispatch_allows_audited_non_idempotent_reconciliation_retry(monkeypatch):
    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    tenant_id = uuid.uuid4()
    parent_user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    child_session_id = uuid.uuid4()
    calls: dict[str, object] = {}

    async def fake_get_runtime_task_record(task_id):
        assert task_id == run_id
        return {
            "task_id": run_id,
            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
            "status": "pending",
            "tenant_id": str(tenant_id),
            "parent_agent_id": str(parent),
            "root_user_id": str(parent_user_id),
            "child_agent_name": "general-purpose",
            "prompt": "retry approved work",
            "trace_id": "trace-subagent",
            "parent_session_id": str(parent_session_id),
            "child_session_id": str(child_session_id),
            "metadata": {
                "subagent_type": "general-purpose",
                "subagent_name": "general-purpose",
                "resume_after_restart": True,
                "resumable_subagent": True,
                "reconciliation_status": "retry_requested",
                "reconciliation_retry_allowed": True,
                "reconciliation_retry_contract": {
                    "schema": "runtime_reconciliation_retry_contract.v1",
                    "kind": "audited_subagent_restart_retry",
                    "task_type": "subagent",
                    "task_id": run_id,
                    "blocker": "non_idempotent_subagent_type",
                    "requires_human_approval": True,
                    "retry_mode": "restart_from_prompt",
                    "side_effect_risk": "mutating",
                },
            },
        }

    async def fake_load_subagent_resume_messages(**_kwargs):
        return []

    async def fake_resolve_parent_runtime(parent_agent_id, **_authority):
        calls["resolved_parent"] = parent_agent_id
        return {
            "ctx_kwargs": {
                "parent_agent_id": parent_agent_id,
                "parent_user_id": parent_user_id,
                "model": object(),
                "parent_agent_name": "Parent",
                "tenant_id": tenant_id,
            }
        }

    async def fake_spawn_subagent(ctx, spec, task, **kwargs):
        calls["ctx"] = ctx
        calls["spec"] = spec
        calls["task"] = task
        calls["kwargs"] = kwargs
        return SimpleNamespace(
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="retried")
        )

    async def fake_update_runtime_task_record(task_id, **kwargs):
        calls.setdefault("updates", []).append((task_id, kwargs))
        return True

    async def fake_update_child_session(**kwargs):
        calls["child_session_update"] = kwargs

    monkeypatch.setattr(svc, "get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr(svc, "_load_subagent_resume_messages", fake_load_subagent_resume_messages, raising=False)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(svc, "update_subagent_child_session_state_for_run", fake_update_child_session)

    dispatched = await svc.dispatch_persisted_subagent_run(run_id)

    assert dispatched is True
    assert calls["resolved_parent"] == parent
    assert calls["spec"].type == "general-purpose"
    assert calls["task"] == "retry approved work"
    assert calls["kwargs"]["resume_messages"] is None
    assert calls["updates"][0][1]["status"] == "running"
    assert calls["updates"][-1][1]["status"] == "completed"


@pytest.mark.asyncio
async def test_dispatch_persisted_subagent_run_restores_model_resolver_for_real_spawn(monkeypatch):
    from app.agents import subagent as subagent_core

    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    tenant_id = uuid.uuid4()
    parent_user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    child_session_id = uuid.uuid4()
    parent_model = SimpleNamespace(provider="openai", model="parent", api_key="k", base_url=None)
    child_model = SimpleNamespace(provider="openai", model="child", api_key="k", base_url=None)
    calls: dict[str, object] = {}

    async def fake_get_runtime_task_record(task_id):
        assert task_id == run_id
        return {
            "task_id": run_id,
            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
            "status": "running",
            "tenant_id": str(tenant_id),
            "parent_agent_id": str(parent),
            "root_user_id": str(parent_user_id),
            "child_agent_name": "code-reviewer",
            "prompt": "review x",
            "trace_id": "trace-subagent",
            "parent_session_id": str(parent_session_id),
            "child_session_id": str(child_session_id),
            "metadata": {
                "subagent_type": "critic",
                "subagent_name": "code-reviewer",
                "resume_after_restart": True,
                "resumable_subagent": True,
                "subagent_spec": {
                    "name": "code-reviewer",
                    "description": "Use for code review.",
                    "type": "critic",
                    "allowed_tools": ["read_file"],
                    "model": "child-model",
                    "isolation": "none",
                },
            },
        }

    async def fake_resolve_parent_runtime(parent_agent_id, **_authority):
        assert parent_agent_id == parent
        return SubagentSpawnContext(
            parent_agent_id=parent,
            parent_user_id=parent_user_id,
            model=parent_model,
            parent_agent_name="Parent",
            tenant_id=tenant_id,
        )

    async def fake_resolve_model_override(model_name, override_tenant_id):
        calls["model_override"] = (model_name, override_tenant_id)
        return child_model

    async def fake_invoke(request):
        calls["request"] = request
        return SimpleNamespace(content="review done", tokens_used=3)

    async def real_spawn_with_fake_invoke(ctx, spec, task, **kwargs):
        calls["ctx"] = ctx
        return await subagent_core.spawn_subagent(ctx, spec, task, **kwargs, invoke=fake_invoke)

    async def fake_update_runtime_task_record(task_id, **kwargs):
        calls.setdefault("updates", []).append((task_id, kwargs))
        return True

    async def fake_update_child_session(**kwargs):
        calls["child_session_update"] = kwargs

    monkeypatch.setattr(svc, "get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "_resolve_model_override", fake_resolve_model_override, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", real_spawn_with_fake_invoke, raising=False)
    monkeypatch.setattr(subagent_core, "_append_subagent_t0_event", lambda **_kwargs: None)
    monkeypatch.setattr(subagent_core, "_seal_subagent_t0_segment", lambda **_kwargs: None)
    monkeypatch.setattr(subagent_core, "_emit_subagent_lifecycle_hook", lambda **_kwargs: None)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(svc, "update_subagent_child_session_state_for_run", fake_update_child_session)

    dispatched = await svc.dispatch_persisted_subagent_run(run_id)

    assert dispatched is True
    assert calls["model_override"] == ("child-model", tenant_id)
    assert calls["request"].model is child_model
    assert calls["updates"][-1][1]["status"] == "completed"
    assert calls["child_session_update"]["status"] == "completed"


@pytest.mark.asyncio
async def test_dispatch_persisted_subagent_run_restores_memory_and_fork_context(monkeypatch, tmp_path):
    from app.agents import subagent as subagent_core
    from app.agents import subagent_memory as memory_mod

    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    tenant_id = uuid.uuid4()
    parent_user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    child_session_id = uuid.uuid4()
    parent_model = SimpleNamespace(provider="openai", model="parent", api_key="k", base_url=None)
    memory_store = SubagentMemoryStore(tmp_path / "subagent-memory")
    memory_store.record_how("researcher", "Prefer primary filings.", category="source_calibration")
    calls: dict[str, object] = {}

    async def allowed_memory_write(content, **_kwargs):
        return SimpleNamespace(
            rejected=False,
            reason="",
            content=content,
            metadata={"entry_id": "worker-lesson", "sensitivity": "internal"},
        )

    async def fake_get_runtime_task_record(task_id):
        assert task_id == run_id
        return {
            "task_id": run_id,
            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
            "status": "running",
            "tenant_id": str(tenant_id),
            "parent_agent_id": str(parent),
            "root_user_id": str(parent_user_id),
            "child_agent_name": "researcher",
            "prompt": "continue research",
            "trace_id": "trace-subagent",
            "parent_session_id": str(parent_session_id),
            "child_session_id": str(child_session_id),
            "metadata": {
                "subagent_type": "explorer",
                "subagent_name": "researcher",
                "resume_after_restart": True,
                "resumable_subagent": True,
                "definition_scope": "agent",
                "subagent_spec": {
                    "name": "researcher",
                    "description": "Use for research.",
                    "type": "explorer",
                    "allowed_tools": ["read_file"],
                    "isolation": "all",
                    "memory_scope": "project",
                },
            },
        }

    async def fake_resolve_parent_runtime(parent_agent_id, **_authority):
        assert parent_agent_id == parent
        return SubagentSpawnContext(
            parent_agent_id=parent,
            parent_user_id=parent_user_id,
            model=parent_model,
            parent_agent_name="Parent",
            tenant_id=tenant_id,
        )

    async def fake_load_parent_messages_for_fork(**kwargs):
        calls["parent_message_loader"] = kwargs
        return [{"role": "user", "content": "parent context survives restart"}]

    async def fake_invoke(request):
        calls["request"] = request
        return SimpleNamespace(content="new research lesson", tokens_used=5)

    async def real_spawn_with_fake_invoke(ctx, spec, task, **kwargs):
        calls["ctx"] = ctx
        return await subagent_core.spawn_subagent(ctx, spec, task, **kwargs, invoke=fake_invoke)

    async def fake_update_runtime_task_record(task_id, **kwargs):
        calls.setdefault("updates", []).append((task_id, kwargs))
        return True

    async def fake_update_child_session(**kwargs):
        calls["child_session_update"] = kwargs

    monkeypatch.setattr(memory_mod, "prepare_memory_write_with_llm", allowed_memory_write)
    monkeypatch.setattr(svc, "get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "_load_parent_messages_for_fork", fake_load_parent_messages_for_fork, raising=False)
    monkeypatch.setattr(svc, "memory_store_for_agent", lambda _agent_id: memory_store, raising=False)
    monkeypatch.setattr(svc, "memory_store_for_tenant", lambda _tenant_id: None, raising=False)
    monkeypatch.setattr(
        svc,
        "make_llm_how_distiller",
        lambda *_args, **_kwargs: lambda _run_log: [("pitfall", "Avoid stale restart assumptions.")],
        raising=False,
    )
    monkeypatch.setattr(svc, "spawn_subagent", real_spawn_with_fake_invoke, raising=False)
    monkeypatch.setattr(subagent_core, "_append_subagent_t0_event", lambda **_kwargs: None)
    monkeypatch.setattr(subagent_core, "_seal_subagent_t0_segment", lambda **_kwargs: None)
    monkeypatch.setattr(subagent_core, "_emit_subagent_lifecycle_hook", lambda **_kwargs: None)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(svc, "update_subagent_child_session_state_for_run", fake_update_child_session)

    dispatched = await svc.dispatch_persisted_subagent_run(run_id)

    assert dispatched is True
    assert calls["request"].messages[0]["content"] == "parent context survives restart"
    assert "Subagent Memory" in calls["request"].standalone_system_prompt
    assert "Prefer primary filings." in calls["request"].standalone_system_prompt
    assert "Avoid stale restart assumptions." in memory_store.load("researcher")
    assert calls["parent_message_loader"]["session_id"] == str(parent_session_id)
    assert calls["updates"][-1][1]["status"] == "completed"


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_reconciles_general_purpose_without_child_transcript_resume(monkeypatch):
    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    calls: dict[str, object] = {}

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running"), task_types=None):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "status": "pending",
                "parent_agent_id": str(parent),
                "child_agent_name": "analyst",
                "prompt": "summarize this report",
                "trace_id": "trace-subagent",
                "parent_session_id": "parent-session",
                "metadata": {
                    "subagent_type": "general-purpose",
                    "subagent_name": "analyst",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                    "side_effect_risk": "mutating",
                    "restart_replay_contract": {
                        "schema": "runtime_restart_replay_contract.v1",
                        "idempotency_key": f"subagent:{run_id}:restart",
                        "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                    },
                    "restart_replay_journal": [
                        {
                            "phase": "spawn_intent_recorded",
                            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                            "task_id": run_id,
                        }
                    ],
                },
            }
        ]

    async def fake_resolve_parent_runtime(_parent_agent_id, **_authority):
        raise AssertionError("startup resume must enqueue; worker dispatch resolves runtime")

    async def fake_spawn_subagent(*_args, **_kwargs):
        raise AssertionError("startup resume must not spawn in the startup process")

    async def fake_update_runtime_task_record(task_id, **kwargs):
        calls.setdefault("updates", []).append((task_id, kwargs))
        return True

    async def fake_notify(**kwargs):  # pragma: no cover - fail-closed runs must not wake worker
        calls["notify"] = kwargs

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", fake_notify)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == []
    assert calls["updates"][-1][1]["status"] == "needs_reconciliation"
    assert calls["updates"][-1][1]["metadata_json"]["needs_reconciliation"] is True
    assert calls["updates"][-1][1]["metadata_json"]["restart_resume_blocker"] == "non_idempotent_subagent_type"
    decision = calls["updates"][-1][1]["metadata_json"]["subagent_decision_entry"]
    assert decision["schema"] == "hive.ccplus.subagent_decision_entry.v1"
    assert decision["replay_mode"] == "blocked"
    assert decision["blocker"] == "non_idempotent_subagent_type"
    assert decision["safe_to_retry"] is False
    assert decision["retry_available"] is True
    assert decision["required_user_action"] == "approve_reconciliation_retry"
    assert calls["updates"][-1][1]["metadata_json"]["reconciliation_retry_allowed"] is True
    assert calls["updates"][-1][1]["metadata_json"]["reconciliation_retry_contract"] == {
        "schema": "runtime_reconciliation_retry_contract.v1",
        "kind": "audited_subagent_restart_retry",
        "task_type": "subagent",
        "task_id": run_id,
        "blocker": "non_idempotent_subagent_type",
        "requires_human_approval": True,
        "retry_mode": "restart_from_prompt",
        "side_effect_risk": "mutating",
    }
    assert calls["updates"][-1][1]["metadata_json"]["subagent_type"] == "general-purpose"
    assert "notify" not in calls


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_recovers_false_positive_reconciliation_without_known_tool_frames(
    monkeypatch,
):
    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    calls: dict[str, object] = {}

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running"), task_types=None):
        assert statuses == ("pending", "running", "needs_reconciliation")
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "status": "needs_reconciliation",
                "parent_agent_id": str(parent),
                "child_agent_name": "analyst",
                "prompt": "summarize this report",
                "trace_id": "trace-subagent",
                "parent_session_id": "parent-session",
                "metadata": {
                    "subagent_type": "general-purpose",
                    "subagent_name": "analyst",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                    "orphaned_by_restart": True,
                    "restart_resume_blocker": "non_idempotent_subagent_type",
                    "side_effect_risk": "mutating",
                    "restart_replay_contract": {
                        "schema": "runtime_restart_replay_contract.v1",
                        "idempotency_key": f"subagent:{run_id}:restart",
                        "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                    },
                    "restart_replay_journal": [
                        {
                            "phase": "spawn_intent_recorded",
                            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                            "task_id": run_id,
                        }
                    ],
                },
            }
        ]

    async def fake_resolve_parent_runtime(_parent_agent_id, **_authority):
        raise AssertionError("startup resume must enqueue; worker dispatch resolves runtime")

    async def fake_spawn_subagent(*_args, **_kwargs):
        raise AssertionError("startup resume must not spawn in the startup process")

    async def fake_update_runtime_task_record(task_id, **kwargs):
        calls.setdefault("updates", []).append((task_id, kwargs))
        return True

    async def fake_notify(**kwargs):
        calls["notify"] = kwargs

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", fake_notify)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == []
    assert "updates" not in calls
    assert "notify" not in calls


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_reconciles_general_purpose_with_readonly_last_frame(monkeypatch):
    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    updates: list[tuple[str, dict]] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running"), task_types=None):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "status": "pending",
                "parent_agent_id": str(parent),
                "child_agent_name": "analyst",
                "prompt": "write file then inspect it",
                "trace_id": "trace-subagent",
                "parent_session_id": "parent-session",
                "metadata": {
                    "subagent_type": "general-purpose",
                    "subagent_name": "analyst",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                    "side_effect_risk": "mutating",
                    "restart_replay_contract": {
                        "schema": "runtime_restart_replay_contract.v1",
                        "idempotency_key": f"subagent:{run_id}:restart",
                        "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                    },
                    "restart_replay_journal": [
                        {
                            "phase": "spawn_intent_recorded",
                            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                            "task_id": run_id,
                        }
                    ],
                    "last_child_tool_frame": {
                        "tool_call_id": "call-read",
                        "tool_name": "read_file",
                        "arguments": {"path": "workspace/a.md"},
                        "status": "done",
                        "origin_channel": "subagent",
                        "subagent_run_id": run_id,
                    },
                },
            }
        ]

    async def fake_spawn_subagent(*_args, **_kwargs):  # pragma: no cover - mutating worker must not replay
        raise AssertionError("general-purpose replay must fail closed without transcript continuation")

    async def fake_resolve_parent_runtime(
        _parent_agent_id, **_authority
    ):  # pragma: no cover - must not resolve runtime
        raise AssertionError("general-purpose replay must fail closed before runtime resolution")

    async def fake_update_runtime_task_record(task_id, **kwargs):
        updates.append((task_id, kwargs))
        return True

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == []
    assert updates[-1][0] == run_id
    assert updates[-1][1]["status"] == "needs_reconciliation"
    assert updates[-1][1]["metadata_json"]["restart_resume_blocker"] == "non_idempotent_subagent_type"


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_restores_readonly_child_pending_frame(monkeypatch):
    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    child_session_id = str(uuid.uuid4())
    calls: dict[str, object] = {}

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running"), task_types=None):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "status": "pending",
                "parent_agent_id": str(parent),
                "child_agent_name": "scout",
                "prompt": "read x",
                "trace_id": "trace-subagent",
                "parent_session_id": "parent-session",
                "child_session_id": child_session_id,
                "metadata": {
                    "subagent_type": "explorer",
                    "subagent_name": "scout",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                    "child_pending_tool_frame": {
                        "tool_call_id": "call-read",
                        "tool_name": "read_file",
                        "arguments": {"path": "workspace/a.md"},
                        "status": "running",
                        "origin_channel": "subagent",
                    },
                },
            }
        ]

    async def fake_resolve_parent_runtime(_parent_agent_id, **_authority):
        raise AssertionError("startup resume must enqueue; worker dispatch resolves runtime")

    async def fake_spawn_subagent(*_args, **_kwargs):
        raise AssertionError("startup resume must not spawn in the startup process")

    async def fake_update_runtime_task_record(task_id, **kwargs):
        calls.setdefault("updates", []).append((task_id, kwargs))
        return True

    async def fake_notify(**kwargs):
        calls["notify"] = kwargs

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "requeue_runtime_task_for_worker", fake_update_runtime_task_record)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", fake_notify)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == [run_id]
    assert calls["updates"][-1][1]["task_type"] == svc.SUBAGENT_RUN_TASK_TYPE
    assert calls["updates"][-1][1]["metadata_json"]["child_frame_recovered_after_restart"] is True
    recovery = calls["updates"][-1][1]["metadata_json"]["recovery_metadata"]
    assert recovery["pending_tool_frame"]["tool_call_id"] == "call-read"
    assert recovery["pending_tool_frame"]["tool_name"] == "read_file"
    assert recovery["pending_tool_frame"]["subagent_run_id"] == run_id
    assert calls["updates"][-1][1]["metadata_json"]["restart_replay_journal"][-1]["phase"] == (
        "child_frame_resume_intent_recorded"
    )
    assert calls["notify"]["runtime_task_id"] == run_id


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_reconciles_mutating_child_pending_frame(monkeypatch):
    run_id = uuid.uuid4().hex
    tenant_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    parent_user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    child_session_id = uuid.uuid4()
    updates: list[tuple[str, dict]] = []
    session_updates: list[dict] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running"), task_types=None):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "status": "pending",
                "tenant_id": str(tenant_id),
                "parent_agent_id": str(parent_agent_id),
                "root_user_id": str(parent_user_id),
                "child_agent_name": "worker",
                "prompt": "write x",
                "trace_id": "trace-subagent",
                "parent_session_id": str(parent_session_id),
                "child_session_id": str(child_session_id),
                "metadata": {
                    "subagent_type": "explorer",
                    "subagent_name": "worker",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                    "child_pending_tool_frame": {
                        "tool_call_id": "call-write",
                        "tool_name": "write_file",
                        "arguments": {"path": "workspace/a.md", "content": "x"},
                        "status": "running",
                        "origin_channel": "subagent",
                    },
                },
            }
        ]

    async def fake_get_runtime_task_record(task_id):
        assert task_id == run_id
        return (await fake_list_active_runtime_task_records())[0]

    async def fake_spawn_subagent(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("mutating child pending frame must not replay")

    async def fake_update_runtime_task_record(task_id, **kwargs):
        updates.append((task_id, kwargs))
        return True

    async def fake_update_child_session_state(**kwargs):
        session_updates.append(kwargs)

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(svc, "update_subagent_child_session_state_for_run", fake_update_child_session_state)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == []
    assert updates[-1][0] == run_id
    assert updates[-1][1]["status"] == "needs_reconciliation"
    assert updates[-1][1]["metadata_json"]["restart_resume_blocker"] == "child_pending_tool_frame_not_replay_safe"
    decision = updates[-1][1]["metadata_json"]["subagent_decision_entry"]
    assert decision["schema"] == "hive.ccplus.subagent_decision_entry.v1"
    assert decision["replay_mode"] == "blocked"
    assert decision["blocker"] == "child_pending_tool_frame_not_replay_safe"
    assert decision["safe_to_retry"] is False
    assert decision["retry_available"] is False
    assert decision["required_user_action"] == "manual_reconcile_or_abandon"
    assert updates[-1][1]["metadata_json"].get("reconciliation_retry_allowed") is not True
    assert "reconciliation_retry_contract" not in updates[-1][1]["metadata_json"]
    assert updates[-1][1]["metadata_json"]["child_pending_tool_frame"]["tool_name"] == "write_file"
    # Startup terminalization has one durable projection owner: the completion
    # outbox created in the same CAS transaction. Direct session projection
    # here would race and duplicate the outbox pump after a process restart.
    assert session_updates == []


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_marks_mutating_record_for_reconciliation(monkeypatch):
    run_id = uuid.uuid4().hex
    updates: list[tuple[str, dict]] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running"), task_types=None):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "status": "pending",
                "parent_agent_id": str(uuid.uuid4()),
                "child_agent_name": "worker",
                "prompt": "write x",
                "metadata": {
                    "subagent_type": "worker",
                    "subagent_name": "worker",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                },
            }
        ]

    async def fake_spawn_subagent(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("mutating subagent must not be replayed")

    async def fake_update_runtime_task_record(task_id, **kwargs):
        updates.append((task_id, kwargs))
        return True

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == []
    assert updates[-1][0] == run_id
    assert updates[-1][1]["status"] == "needs_reconciliation"
    assert updates[-1][1]["metadata_json"]["needs_reconciliation"] is True
    assert updates[-1][1]["metadata_json"]["side_effect_risk"] == "mutating"


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_reconciles_mutating_worker_even_with_spawn_journal(monkeypatch):
    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    updates: list[tuple[str, dict]] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running"), task_types=None):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "status": "pending",
                "parent_agent_id": str(parent),
                "child_agent_name": "worker",
                "prompt": "write x",
                "trace_id": "trace-subagent",
                "parent_session_id": "parent-session",
                "metadata": {
                    "subagent_type": "worker",
                    "subagent_name": "worker",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                    "side_effect_risk": "mutating",
                    "restart_replay_contract": {
                        "schema": "runtime_restart_replay_contract.v1",
                        "idempotency_key": f"subagent:{run_id}:restart",
                        "task_type": "subagent",
                    },
                    "restart_replay_journal": [
                        {
                            "schema": "runtime_restart_replay_journal.v1",
                            "idempotency_key": f"subagent:{run_id}:restart:spawn_intent_recorded",
                            "task_type": "subagent",
                            "task_id": run_id,
                            "phase": "spawn_intent_recorded",
                            "side_effect_risk": "mutating",
                        }
                    ],
                    "last_child_tool_frame": {
                        "tool_call_id": "call-write",
                        "tool_name": "write_file",
                        "arguments": {"path": "workspace/a.md", "content": "x"},
                        "status": "done",
                        "origin_channel": "subagent",
                        "subagent_run_id": run_id,
                    },
                },
            }
        ]

    async def fake_resolve_parent_runtime(_parent_agent_id, **_authority):  # pragma: no cover - must not run
        raise AssertionError("mutating subagent must not resolve runtime for replay")

    async def fake_spawn_subagent(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("mutating subagent must not be replayed from spawn intent")

    async def fake_update_runtime_task_record(task_id, **kwargs):
        updates.append((task_id, kwargs))
        return True

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == []
    assert updates[-1][0] == run_id
    assert updates[-1][1]["status"] == "needs_reconciliation"
    assert updates[-1][1]["metadata_json"]["needs_reconciliation"] is True
    assert updates[-1][1]["metadata_json"]["restart_resume_blocker"] == "child_tool_frame_not_replay_safe"


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_refuses_mutating_worker_without_replay_journal(monkeypatch):
    run_id = uuid.uuid4().hex
    updates: list[tuple[str, dict]] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running"), task_types=None):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "status": "pending",
                "parent_agent_id": str(uuid.uuid4()),
                "child_agent_name": "worker",
                "prompt": "write x",
                "metadata": {
                    "subagent_type": "worker",
                    "subagent_name": "worker",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                    "side_effect_risk": "mutating",
                    "restart_replay_contract": {
                        "schema": "runtime_restart_replay_contract.v1",
                        "idempotency_key": f"subagent:{run_id}:restart",
                        "task_type": "subagent",
                    },
                },
            }
        ]

    async def fake_spawn_subagent(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("mutating subagent without replay journal must not be replayed")

    async def fake_resolve_parent_runtime(_parent_agent_id, **_authority):
        return {
            "ctx_kwargs": {
                "parent_agent_id": uuid.uuid4(),
                "parent_user_id": uuid.uuid4(),
                "model": object(),
                "parent_agent_name": "Parent",
                "tenant_id": uuid.uuid4(),
            }
        }

    async def fake_update_runtime_task_record(task_id, **kwargs):
        updates.append((task_id, kwargs))
        return True

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == []
    assert updates[-1][0] == run_id
    assert updates[-1][1]["status"] == "needs_reconciliation"
    assert updates[-1][1]["metadata_json"]["restart_resume_blocker"] == "non_idempotent_subagent_type"


@pytest.mark.asyncio
async def test_subagent_cancel_received_before_dispatch_registration_is_applied():
    run_id = uuid.uuid4().hex

    assert svc.apply_remote_subagent_cancel(run_id) is True

    cancel_event = svc._subagent_cancel_event_for_run(run_id)
    try:
        assert cancel_event.is_set() is True
    finally:
        svc._release_subagent_cancel_event(run_id, cancel_event)

    fresh_event = svc._subagent_cancel_event_for_run(run_id)
    try:
        assert fresh_event.is_set() is False
    finally:
        svc._release_subagent_cancel_event(run_id, fresh_event)


@pytest.mark.asyncio
async def test_dispatch_persisted_subagent_run_honors_cancel_arriving_during_hydration(monkeypatch):
    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    tenant_id = uuid.uuid4()
    parent_user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    child_session_id = uuid.uuid4()
    updates: list[tuple[str, dict]] = []

    async def fake_get_runtime_task_record(task_id):
        assert task_id == run_id
        return {
            "task_id": run_id,
            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
            "status": "pending",
            "tenant_id": str(tenant_id),
            "parent_agent_id": str(parent),
            "root_user_id": str(parent_user_id),
            "child_agent_name": "scout",
            "prompt": "read x",
            "trace_id": "trace-subagent",
            "parent_session_id": str(parent_session_id),
            "child_session_id": str(child_session_id),
            "metadata": {
                "subagent_type": "explorer",
                "subagent_name": "scout",
                "resume_after_restart": True,
                "resumable_subagent": True,
            },
        }

    async def fake_load_resume_messages(**_kwargs):
        return []

    async def fake_resolve_parent_runtime(_parent_agent_id, **_authority):
        assert svc.apply_remote_subagent_cancel(run_id) is True
        return {
            "ctx_kwargs": {
                "parent_agent_id": parent,
                "parent_user_id": parent_user_id,
                "model": SimpleNamespace(provider="test", model="fake-model"),
                "parent_agent_name": "Parent",
                "tenant_id": tenant_id,
            }
        }

    async def fake_hydrate_worker_runtime_context(runtime, **_kwargs):
        return runtime

    async def fake_spawn_subagent(runtime, spec, *_args, **_kwargs):
        assert runtime.cancel_event.is_set() is True
        return SimpleNamespace(
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="ignored")
        )

    async def fake_update_runtime_task_record(task_id, **kwargs):
        updates.append((task_id, kwargs))
        return True

    async def fake_update_child_session_state_for_run(**_kwargs):
        return None

    monkeypatch.setattr(svc, "get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr(svc, "_load_subagent_resume_messages", fake_load_resume_messages)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "_hydrate_worker_runtime_context", fake_hydrate_worker_runtime_context)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(svc, "update_subagent_child_session_state_for_run", fake_update_child_session_state_for_run)

    dispatched = await svc.dispatch_persisted_subagent_run(run_id)

    assert dispatched is True
    assert updates[-1][0] == run_id
    assert updates[-1][1]["status"] == "killed"
    assert updates[-1][1]["metadata_json"]["cancelled"] is True
