from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed_hr_draft(owner_sessionmaker, *, draft_status: str = "awaiting_confirmation"):
    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.hr_creation import HrCreationDraft
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id = uuid4()
    user_id = uuid4()
    hr_agent_id = uuid4()
    session_id = uuid4()
    draft_id = uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="HR runtime", slug=f"hr-runtime-{tenant_id.hex[:8]}"))
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        user = User(
            id=user_id,
            username=f"hr-runtime-{user_id.hex[:8]}",
            email=f"{user_id.hex[:8]}@hr-runtime.test",
            password_hash="x",
            display_name="HR Owner",
            tenant_id=tenant_id,
        )
        hr_agent = Agent(
            id=hr_agent_id,
            tenant_id=tenant_id,
            name="__system_hr__",
            role_description="System HR",
            creator_id=user_id,
            sponsor_user_id=user_id,
            owner_user_id=user_id,
            agent_class="internal_system",
            status="running",
        )
        db.add_all([user, hr_agent])
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=hr_agent_id, tenant_id=tenant_id, user_id=user_id))
        db.add(
            HrCreationDraft(
                id=draft_id,
                tenant_id=tenant_id,
                hr_agent_id=hr_agent_id,
                session_id=session_id,
                requested_by_user_id=user_id,
                status=draft_status,
                blueprint_version=3,
                blueprint_hash="sha256:hr-runtime",
                blueprint_json={"name": "Researcher", "role_description": "Research markets."},
                preview_json={"status": "preview", "missing_gates": []},
            )
        )
    return tenant_id, user_id, hr_agent_id, session_id, draft_id


@pytest.mark.asyncio
async def test_confirm_api_atomically_enqueues_one_hr_runtime_task_on_duplicate_confirmation(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from sqlalchemy import select

    import app.database as database
    from app.api.hr_creation import HrCreationConfirmIn, confirm_hr_creation_draft
    from app.models.hr_creation import HrCreationDraft
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User

    tenant_id, user_id, hr_agent_id, _, draft_id = await _seed_hr_draft(owner_sessionmaker)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)

    async def no_audit(*_args, **_kwargs):
        return None

    wakeups: list[str] = []

    async def capture_wakeup(*, reason, runtime_task_id):
        assert reason == "hr_provisioning_queued"
        wakeups.append(str(runtime_task_id))

    monkeypatch.setattr("app.core.policy.write_audit_event", no_audit)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", capture_wakeup)

    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        assert user is not None
        payload = HrCreationConfirmIn(blueprint_version=3, blueprint_hash="sha256:hr-runtime")
        first = await confirm_hr_creation_draft(hr_agent_id, draft_id, payload, user, db)
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        assert user is not None
        second = await confirm_hr_creation_draft(hr_agent_id, draft_id, payload, user, db)

    assert first.provisioning_task_id == second.provisioning_task_id
    assert wakeups == [first.provisioning_task_id]
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        tasks = (
            (
                await db.execute(
                    select(RuntimeTask).where(RuntimeTask.root_idempotency_key == f"hr-provisioning:{draft_id}-v3")
                )
            )
            .scalars()
            .all()
        )
        assert draft is not None
        assert len(tasks) == 1
        assert draft.provisioning_task_id == tasks[0].id
        assert tasks[0].task_type == "hr_provisioning"
        assert tasks[0].status == "pending"
        assert tasks[0].tenant_id == tenant_id


@pytest.mark.asyncio
async def test_hr_worker_converges_completed_draft_without_duplicate_provisioning(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    import app.database as database
    from app.models.hr_creation import HrCreationDraft
    from app.models.runtime_task import RuntimeTask
    from app.services.hr_provisioning_runtime import (
        build_hr_provisioning_runtime_task,
        execute_claimed_hr_provisioning,
    )

    tenant_id, _, hr_agent_id, _, draft_id = await _seed_hr_draft(
        owner_sessionmaker,
        draft_status="confirmed",
    )
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft is not None
        draft.confirmed_by_user_id = draft.requested_by_user_id
        draft.confirmed_at = datetime.now(timezone.utc)
        task = build_hr_provisioning_runtime_task(draft)
        task.status = "running"
        task.claimed_by = "hr-test-worker"
        task.claim_version = 1
        db.add(task)
        await db.flush()
        draft.provisioning_task_id = task.id
        await db.commit()
        task_id = task.id
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    calls: list[str] = []

    async def fake_run(*, task, draft):
        calls.append(str(draft.id))
        async with owner_sessionmaker() as db:
            persisted = await db.get(HrCreationDraft, draft.id)
            assert persisted is not None
            persisted.status = "completed"
            persisted.created_agent_id = hr_agent_id
            await db.commit()
        return "created"

    monkeypatch.setattr("app.services.hr_provisioning_runtime._run_domain_provisioning", fake_run)

    assert await execute_claimed_hr_provisioning(task_id) == "completed"
    assert await execute_claimed_hr_provisioning(task_id) == "completed"
    assert calls == [str(draft_id)]
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, task_id)
        assert task is not None
        assert task.status == "completed"
        assert task.metadata_json["outcome"]["status"] == "completed"
        assert task.tenant_id == tenant_id


@pytest.mark.asyncio
async def test_reclaimed_hr_job_waits_for_unexpired_draft_claim_instead_of_double_running(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    import app.database as database
    from app.models.hr_creation import HrCreationDraft
    from app.models.runtime_task import RuntimeTask
    from app.services.hr_provisioning_runtime import (
        build_hr_provisioning_runtime_task,
        execute_claimed_hr_provisioning,
    )

    _, _, _, _, draft_id = await _seed_hr_draft(owner_sessionmaker, draft_status="creating")
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft is not None
        draft.claim_token = uuid4()
        draft.claim_version = 4
        draft.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        task = build_hr_provisioning_runtime_task(draft)
        task.status = "running"
        task.claimed_by = "reclaimer"
        task.claim_version = 2
        task.metadata_json = {**dict(task.metadata_json or {}), "reclaimed_expired_claim": True}
        db.add(task)
        await db.flush()
        draft.provisioning_task_id = task.id
        await db.commit()
        task_id = task.id
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)

    async def forbidden_run(**_kwargs):
        raise AssertionError("an unexpired draft claim must not be double-run")

    monkeypatch.setattr("app.services.hr_provisioning_runtime._run_domain_provisioning", forbidden_run)

    assert await execute_claimed_hr_provisioning(task_id) == "waiting_claim_expiry"
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, task_id)
        assert task is not None
        assert task.status == "resumable"
        assert task.scheduled_at is not None
        assert task.metadata_json["phase"] == "waiting_draft_claim_expiry"


@pytest.mark.asyncio
async def test_failed_hr_job_can_be_retried_without_model_or_new_task(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    import app.database as database
    from app.api.hr_creation import retry_hr_creation_draft
    from app.models.hr_creation import HrCreationDraft
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.services.hr_provisioning_runtime import build_hr_provisioning_runtime_task

    _, user_id, hr_agent_id, _, draft_id = await _seed_hr_draft(owner_sessionmaker, draft_status="failed")
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft is not None
        draft.confirmed_by_user_id = user_id
        draft.confirmed_at = datetime.now(timezone.utc)
        draft.failure_code = "provider_timeout"
        task = build_hr_provisioning_runtime_task(draft)
        task.status = "failed"
        task.completed_at = datetime.now(timezone.utc)
        db.add(task)
        await db.flush()
        draft.provisioning_task_id = task.id
        await db.commit()
        task_id = task.id
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    wakeups: list[str] = []
    audit_events: list[str] = []

    async def capture_wakeup(*, reason, runtime_task_id):
        assert reason == "hr_provisioning_retry"
        wakeups.append(str(runtime_task_id))

    async def capture_audit(_db, *, event_type, **_kwargs):
        audit_events.append(event_type)

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", capture_wakeup)
    monkeypatch.setattr("app.core.policy.write_audit_event", capture_audit)
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        assert user is not None
        result = await retry_hr_creation_draft(hr_agent_id, draft_id, user, db)

    assert result.provisioning_task_id == str(task_id)
    assert wakeups == [str(task_id)]
    assert audit_events == ["hr.creation_provisioning_retried"]
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        task = await db.get(RuntimeTask, task_id)
        assert draft is not None and task is not None
        assert draft.status == "confirmed"
        assert draft.failure_code is None
        assert task.status == "resumable"
        assert task.completed_at is None
        assert task.metadata_json["phase"] == "retry_queued"


@pytest.mark.asyncio
async def test_cancelling_running_hr_job_fences_both_task_and_draft_for_reconciliation(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    import app.database as database
    from app.api.hr_creation import cancel_hr_creation_draft
    from app.models.hr_creation import HrCreationDraft
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.services.hr_provisioning_runtime import build_hr_provisioning_runtime_task

    _, user_id, hr_agent_id, _, draft_id = await _seed_hr_draft(owner_sessionmaker, draft_status="creating")
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft is not None
        draft.confirmed_by_user_id = user_id
        draft.confirmed_at = datetime.now(timezone.utc)
        draft.claim_token = uuid4()
        draft.claim_version = 2
        draft.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        task = build_hr_provisioning_runtime_task(draft)
        task.status = "running"
        task.claimed_by = "hr-worker"
        task.claim_version = 7
        db.add(task)
        await db.flush()
        draft.provisioning_task_id = task.id
        await db.commit()
        task_id = task.id
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    audit_events: list[str] = []

    async def capture_audit(_db, *, event_type, **_kwargs):
        audit_events.append(event_type)

    monkeypatch.setattr("app.core.policy.write_audit_event", capture_audit)

    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        assert user is not None
        result = await cancel_hr_creation_draft(hr_agent_id, draft_id, user, db)

    assert result.draft_status == "failed"
    assert audit_events == ["hr.creation_provisioning_cancelled"]
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        task = await db.get(RuntimeTask, task_id)
        assert draft is not None and task is not None
        assert task.status == "needs_reconciliation"
        assert task.claim_version == 8
        assert task.metadata_json["automatic_retry_allowed"] is False
        assert draft.claim_token is None
        assert draft.failure_code == "cancellation_reconciliation_required"


@pytest.mark.asyncio
async def test_hr_reconciler_expires_stale_preview_once(owner_sessionmaker, monkeypatch) -> None:
    import app.database as database
    from app.models.hr_creation import HrCreationDraft
    from app.services.hr_creation_reconciliation import reconcile_hr_creation_drafts_once

    tenant_id, _, _, _, draft_id = await _seed_hr_draft(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft is not None
        draft.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()

    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    first = await reconcile_hr_creation_drafts_once(tenant_id=tenant_id)
    second = await reconcile_hr_creation_drafts_once(tenant_id=tenant_id)

    assert first["expired"] == 1
    assert second["expired"] == 0
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft is not None and draft.status == "expired"


@pytest.mark.asyncio
async def test_hr_reconciler_recreates_missing_job_for_stale_provisioning_draft(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    import app.database as database
    from app.models.hr_creation import HrCreationDraft
    from app.models.runtime_task import RuntimeTask
    from app.services.hr_creation_reconciliation import reconcile_hr_creation_drafts_once

    tenant_id, user_id, _, _, draft_id = await _seed_hr_draft(owner_sessionmaker, draft_status="provisioning")
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft is not None
        draft.confirmed_by_user_id = user_id
        draft.confirmed_at = datetime.now(timezone.utc) - timedelta(hours=1)
        draft.claim_token = uuid4()
        draft.claim_version = 3
        draft.claim_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()

    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    first = await reconcile_hr_creation_drafts_once(tenant_id=tenant_id)
    second = await reconcile_hr_creation_drafts_once(tenant_id=tenant_id)

    assert first["jobs_created"] == 1
    assert second["jobs_created"] == 0
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft is not None and draft.provisioning_task_id is not None
        task = await db.get(RuntimeTask, draft.provisioning_task_id)
        assert task is not None and task.status == "pending"
        assert draft.claim_token is None
        assert draft.claim_version == 4


@pytest.mark.asyncio
async def test_hr_reconciler_never_replays_orphan_without_confirmation_evidence(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    import app.database as database
    from app.models.hr_creation import HrCreationDraft
    from app.services.hr_creation_reconciliation import reconcile_hr_creation_drafts_once

    tenant_id, _, _, _, draft_id = await _seed_hr_draft(owner_sessionmaker, draft_status="provisioning")
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)

    result = await reconcile_hr_creation_drafts_once(tenant_id=tenant_id)

    assert result["authority_blocked"] == 1
    assert result["jobs_created"] == 0
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft is not None
        assert draft.status == "failed"
        assert draft.failure_code == "missing_confirmation_evidence"
        assert draft.provisioning_task_id is None


@pytest.mark.asyncio
async def test_hr_reconciler_converges_failed_task_and_orphan_agent(owner_sessionmaker, monkeypatch) -> None:
    import app.database as database
    from app.models.agent import Agent
    from app.models.hr_creation import HrCreationDraft
    from app.services.hr_creation_reconciliation import reconcile_hr_creation_drafts_once
    from app.services.hr_provisioning_runtime import build_hr_provisioning_runtime_task

    tenant_id, user_id, _, _, draft_id = await _seed_hr_draft(owner_sessionmaker, draft_status="provisioning")
    employee_id = uuid4()
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft is not None
        draft.confirmed_by_user_id = user_id
        draft.confirmed_at = datetime.now(timezone.utc) - timedelta(hours=1)
        draft.claim_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        employee = Agent(
            id=employee_id,
            tenant_id=tenant_id,
            name="Interrupted Employee",
            role_description="Needs recovery",
            creator_id=user_id,
            sponsor_user_id=user_id,
            owner_user_id=user_id,
            status="creating",
        )
        db.add(employee)
        await db.flush()
        draft.created_agent_id = employee_id
        task = build_hr_provisioning_runtime_task(draft)
        task.status = "failed"
        task.completed_at = datetime.now(timezone.utc)
        task.result_summary = "capability install failed"
        db.add(task)
        await db.flush()
        draft.provisioning_task_id = task.id
        await db.commit()

    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    result = await reconcile_hr_creation_drafts_once(tenant_id=tenant_id)

    assert result["failed_converged"] == 1
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        employee = await db.get(Agent, employee_id)
        assert draft is not None and draft.status == "failed"
        assert draft.failure_code == "runtime_task_failed"
        assert employee is not None and employee.status == "error"


@pytest.mark.asyncio
async def test_recoverable_draft_list_and_abandon_are_direct_user_surfaces(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    import app.database as database
    from app.api.hr_creation import abandon_hr_creation_draft, list_hr_creation_drafts
    from app.models.agent import Agent
    from app.models.hr_creation import HrCreationDraft
    from app.models.user import User
    from app.services.hr_provisioning_runtime import build_hr_provisioning_runtime_task

    tenant_id, user_id, hr_agent_id, session_id, draft_id = await _seed_hr_draft(
        owner_sessionmaker,
        draft_status="failed",
    )
    employee_id = uuid4()
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft is not None
        draft.confirmed_by_user_id = user_id
        draft.confirmed_at = datetime.now(timezone.utc)
        employee = Agent(
            id=employee_id,
            tenant_id=tenant_id,
            name="Recoverable Employee",
            role_description="Interrupted provisioning",
            creator_id=user_id,
            sponsor_user_id=user_id,
            owner_user_id=user_id,
            status="creating",
        )
        db.add(employee)
        await db.flush()
        draft.created_agent_id = employee_id
        task = build_hr_provisioning_runtime_task(draft)
        task.status = "failed"
        task.completed_at = datetime.now(timezone.utc)
        db.add(task)
        await db.flush()
        draft.provisioning_task_id = task.id
        await db.commit()

    monkeypatch.setattr(database, "async_session", owner_sessionmaker)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.agent_manager.agent_manager.remove_container", noop)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.archive_agent_files", noop)
    monkeypatch.setattr("app.services.ai_assets.register_projection", noop)
    monkeypatch.setattr("app.core.policy.write_audit_event", noop)

    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        assert user is not None
        listed = await list_hr_creation_drafts(hr_agent_id, user, db)
        assert len(listed) == 1
        assert listed[0].session_id == str(session_id)
        assert listed[0].recovery["can_retry"] is True
        assert listed[0].recovery["can_resume"] is True
        assert listed[0].recovery["can_abandon"] is True

    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        assert user is not None
        abandoned = await abandon_hr_creation_draft(hr_agent_id, draft_id, user, db)
        assert abandoned.draft_status == "superseded"

    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        employee = await db.get(Agent, employee_id)
        assert draft is not None and draft.status == "superseded"
        assert employee is not None and employee.deleted_at is not None
