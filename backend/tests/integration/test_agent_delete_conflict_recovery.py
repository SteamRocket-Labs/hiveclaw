"""Maintained regressions for typed batch-conflict recovery on HR creation cleanup.

G1 removed the bot DELETE endpoint (digital employees are deactivated, never
deleted), so its endpoint regressions went with it. What stays covered here:
``perform_pending_agent_cleanup`` remains the recovery surface for an
abandoned, unfinished HR-created employee, its file archival stays idempotent
and deferred past the commit, and the HR abandon route keeps mapping typed
batch conflicts to truthful retryable 409s with the same repeat-request
cleanup retry. ``soft_delete_agent`` keeps its advisory-first batch order.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


class _FakeDB:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def rollback(self) -> None:
        self.calls.append("rollback")

    async def commit(self) -> None:
        self.calls.append("commit")


async def test_real_pending_cleanup_marker_round_trip(owner_sessionmaker, monkeypatch):
    """The durable marker survives commit, drives archival once, then clears."""

    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.agent_identity_lifecycle import perform_pending_agent_cleanup
    from app.services.agent_manager import agent_manager

    tenant_id, agent_id, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Cleanup Marker Tenant", slug=f"cm-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"cm-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@cm.test",
                password_hash="x",
                display_name="Cleanup Marker",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Marker Agent", creator_id=user_id))
        await db.commit()

    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        assert agent.deleted_at is None

    archived: list[uuid.UUID] = []

    async def archive_spy(agent_uuid):
        archived.append(agent_uuid)

    monkeypatch.setattr(agent_manager, "archive_agent_files", archive_spy)

    # Cleanup is only owed once the Agent is soft-deleted (filesystem truth).
    before_delete = await perform_pending_agent_cleanup(tenant_id, agent_id)
    assert before_delete["performed"] is False and before_delete["archive_ok"] is None, before_delete
    assert archived == []

    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        agent.deleted_at = datetime.now(UTC)
        await db.commit()

    outcome = await perform_pending_agent_cleanup(tenant_id, agent_id)
    assert outcome == {"performed": True, "archive_ok": True}, outcome
    assert archived == [agent_id]


async def test_failed_cleanup_keeps_marker_for_retry(owner_sessionmaker, monkeypatch):
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.agent_identity_lifecycle import perform_pending_agent_cleanup
    from app.services.agent_manager import agent_manager

    tenant_id, agent_id, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Cleanup Retry Tenant", slug=f"cr-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"cr-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@cr.test",
                password_hash="x",
                display_name="Cleanup Retry",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Retry Agent", creator_id=user_id))
        await db.commit()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        agent.deleted_at = datetime.now(UTC)
        await db.commit()

    calls: list[uuid.UUID] = []

    async def failing_archive(agent_uuid):
        calls.append(agent_uuid)
        if len(calls) == 1:
            raise RuntimeError("maintained: archival unavailable")

    monkeypatch.setattr(agent_manager, "archive_agent_files", failing_archive)
    failed = await perform_pending_agent_cleanup(tenant_id, agent_id)
    assert failed == {"performed": True, "archive_ok": False}, failed

    retried = await perform_pending_agent_cleanup(tenant_id, agent_id)
    assert retried == {"performed": True, "archive_ok": True}, retried
    assert len(calls) == 2


async def test_hr_abandon_defers_file_archival_until_after_commit(owner_sessionmaker, monkeypatch, tmp_path) -> None:
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.hr_creation import HrCreationDraft
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.agent_manager import agent_manager
    from app.services.hr_creation_recovery import abandon_hr_creation

    tenant_id, user_id, agent_id, draft_id = (uuid.uuid4() for _ in range(4))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="HR Abandon Tenant", slug=f"ha-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"ha-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@ha.test",
                password_hash="x",
                display_name="HR Abandon",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="HR Employee", creator_id=user_id))
        await db.flush()
        session_id = uuid.uuid4()
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                title="HR Abandon Session",
            )
        )
        await db.flush()
        db.add(
            HrCreationDraft(
                id=draft_id,
                tenant_id=tenant_id,
                hr_agent_id=agent_id,
                session_id=session_id,
                requested_by_user_id=user_id,
                status="failed",
                blueprint_hash="probe",
            )
        )
        await db.commit()

    archived: list[uuid.UUID] = []

    async def archive_spy(agent_uuid):
        archived.append(agent_uuid)

    monkeypatch.setattr(agent_manager, "archive_agent_files", archive_spy)

    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        draft.created_agent_id = agent_id
        await db.commit()
        _, cleanup_agent_ids = await abandon_hr_creation(db, draft, actor_id=user_id, task=None)
        assert cleanup_agent_ids == [agent_id]
        # Nothing destructive has run yet: the transaction has not committed.
        assert archived == []
        await db.commit()

    from app.services.agent_identity_lifecycle import perform_pending_agent_cleanup

    outcome = await perform_pending_agent_cleanup(tenant_id, agent_id)
    assert outcome == {"performed": True, "archive_ok": True}, outcome
    assert archived == [agent_id]
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        assert agent.deleted_at is not None


async def test_claim_quarantine_settlement_finishes_in_own_transaction(
    owner_sessionmaker, monkeypatch, tmp_path
) -> None:
    """The deferred quarantine settlement stamps the terminal fence post-commit."""

    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_claim_service import _finish_deferred_business_task_quarantines

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        task.task_type = "business_task"
        task.status = "needs_reconciliation"
        task.terminal_boundary_generation = 1
        task.result_summary = "business task projection link is missing or no longer active"
        metadata = dict(task.metadata_json or {})
        metadata["business_task_id"] = str(uuid.uuid4())
        task.metadata_json = metadata
        await db.commit()

    await _finish_deferred_business_task_quarantines([seed["run_id"]])

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        metadata: dict[str, Any] = dict(task.metadata_json or {})
    assert metadata.get("terminal_execution_fence_ref"), metadata
    assert metadata.get("terminal_committed_status") == "needs_reconciliation", metadata


async def _seed_admin_agent(owner_sessionmaker, *, role: str = "org_admin", shared_tenant: bool = True):
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id, user_id, agent_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    other_user_id = uuid.uuid4()
    other_tenant_id = tenant_id if shared_tenant else uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Cleanup Retry Tenant", slug=f"e8-{tenant_id.hex[:8]}", is_active=True))
        if not shared_tenant:
            db.add(
                Tenant(id=other_tenant_id, name="Foreign Tenant", slug=f"e8f-{other_tenant_id.hex[:8]}", is_active=True)
            )
        await db.flush()
        db.add(
            User(
                id=user_id,
                username=f"e8c-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@e8c.test",
                password_hash="x",
                display_name="Eighth Creator",
                tenant_id=tenant_id,
                role="org_admin",
                is_active=True,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id, tenant_id=tenant_id, name="Eighth Retry Agent", creator_id=user_id, owner_user_id=user_id
            )
        )
        await db.flush()
        db.add(
            User(
                id=other_user_id,
                username=f"e8-{other_user_id.hex[:8]}",
                email=f"{other_user_id.hex[:8]}@e8.test",
                password_hash="x",
                display_name="Eighth Admin",
                tenant_id=other_tenant_id,
                role=role,
                is_active=True,
            )
        )
        await db.commit()
    return tenant_id, agent_id, other_user_id


async def _seed_hr_abandon_fixture(owner_sessionmaker):
    from app.models.agent import Agent
    from app.models.hr_creation import HrCreationDraft

    from tests.services.test_hr_provisioning_runtime import _seed_hr_draft

    tenant_id, user_id, hr_agent_id, session_id, draft_id = await _seed_hr_draft(
        owner_sessionmaker, draft_status="failed"
    )
    employee_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        draft.confirmed_by_user_id = user_id
        draft.confirmed_at = datetime.now(UTC)
        db.add(
            Agent(
                id=employee_id,
                tenant_id=tenant_id,
                name="Eighth HR Employee",
                creator_id=user_id,
                sponsor_user_id=user_id,
                owner_user_id=user_id,
                status="creating",
            )
        )
        await db.flush()
        draft.created_agent_id = employee_id
        await db.commit()
    return tenant_id, user_id, hr_agent_id, draft_id, employee_id


async def test_hr_repeat_abandon_after_failed_archival_is_cleanup_only(owner_sessionmaker, monkeypatch):
    """CC7 F2: a repeat abandon re-runs ONLY the still-owed file cleanup.

    The first abandon commits (draft superseded, employee soft-deleted) but
    its post-commit archival fails — the route must surface the partial
    state, and re-issuing the same abandon must retry the archival without
    re-running fencing, retirement, or audit.
    """

    import app.database as database
    from app.api.hr_creation import abandon_hr_creation_draft
    from app.models.security_audit import SecurityAuditEvent
    from app.models.user import User

    tenant_id, user_id, hr_agent_id, draft_id, employee_id = await _seed_hr_abandon_fixture(owner_sessionmaker)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)

    import app.services.agent_manager as agent_manager

    archive_calls: list[uuid.UUID] = []

    async def failing_archive(agent_uuid):
        archive_calls.append(agent_uuid)
        if len(archive_calls) == 1:
            raise RuntimeError("maintained: archival unavailable")

    monkeypatch.setattr(agent_manager.agent_manager, "archive_agent_files", failing_archive)

    from fastapi import HTTPException

    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        with pytest.raises(HTTPException) as exc_info:
            await abandon_hr_creation_draft(hr_agent_id, draft_id, user, db)
    assert exc_info.value.status_code == 503, exc_info.value.status_code
    assert exc_info.value.detail["code"] == "hr_abandon_cleanup_pending", exc_info.value.detail
    assert "committed" in exc_info.value.detail["message"], exc_info.value.detail
    assert archive_calls == [employee_id], archive_calls

    async with owner_sessionmaker() as db:
        audit_count = len(
            (await db.execute(select(SecurityAuditEvent).where(SecurityAuditEvent.resource_id == draft_id)))
            .scalars()
            .all()
        )

    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        result = await abandon_hr_creation_draft(hr_agent_id, draft_id, user, db)
    assert result.draft_status == "superseded", result.draft_status
    assert archive_calls == [employee_id, employee_id], archive_calls

    async with owner_sessionmaker() as db:
        audit_after = len(
            (await db.execute(select(SecurityAuditEvent).where(SecurityAuditEvent.resource_id == draft_id)))
            .scalars()
            .all()
        )
    assert audit_after == audit_count, "the cleanup-only repeat abandon re-ran audit effects"


async def test_hr_abandon_maps_typed_batch_conflicts_to_409(owner_sessionmaker, monkeypatch):
    """CC7 F3: the HR route maps the same typed conflicts as the DELETE route."""

    import app.database as database
    from app.api.hr_creation import abandon_hr_creation_draft
    from app.models.user import User
    from app.services.runtime_terminal_settlement import (
        RuntimeTaskLateAdmissionConflict,
        RuntimeTaskSessionBindingConflict,
    )

    tenant_id, user_id, hr_agent_id, draft_id, employee_id = await _seed_hr_abandon_fixture(owner_sessionmaker)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)

    import app.services.agent_manager as agent_manager

    async def _archive_ok(agent_uuid):
        return None

    monkeypatch.setattr(agent_manager.agent_manager, "archive_agent_files", _archive_ok)

    from fastapi import HTTPException

    for conflict_type, expected_code, expected_retryable in (
        (RuntimeTaskLateAdmissionConflict, "hr_abandon_late_admission_conflict", True),
        (RuntimeTaskSessionBindingConflict, "hr_abandon_session_binding_conflict", False),
    ):

        async def conflicting_soft_delete(*_a, **_k):
            raise conflict_type("injected sustained admission pressure")

        monkeypatch.setattr("app.services.agent_identity_lifecycle.soft_delete_agent", conflicting_soft_delete)
        outcome: dict[str, Any] = {}
        async with owner_sessionmaker() as db:
            user = await db.get(User, user_id)
            try:
                out = await abandon_hr_creation_draft(hr_agent_id, draft_id, user, db)
                outcome = {"result": "ok", "status": out.draft_status}
            except HTTPException as exc:
                outcome = {"result": "http", "status": exc.status_code, "detail": exc.detail}
        assert outcome.get("status") == 409, outcome
        assert outcome["detail"]["code"] == expected_code, outcome
        assert outcome["detail"]["retryable"] is expected_retryable, outcome


async def test_superseded_draft_with_live_employee_stays_conflicted(owner_sessionmaker, monkeypatch):
    """The cleanup-only branch must not make every superseded draft abandonable."""

    import app.services.agent_manager as agent_manager
    from app.api.hr_creation import abandon_hr_creation_draft
    from app.models.hr_creation import HrCreationDraft
    from app.models.user import User
    from fastapi import HTTPException

    tenant_id, user_id, hr_agent_id, draft_id, employee_id = await _seed_hr_abandon_fixture(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        draft.status = "superseded"
        await db.commit()

    async def _archive_spy(agent_uuid):
        raise AssertionError("no cleanup may run for a live-employee superseded draft")

    monkeypatch.setattr(agent_manager.agent_manager, "archive_agent_files", _archive_spy)
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        with pytest.raises(HTTPException) as exc_info:
            await abandon_hr_creation_draft(hr_agent_id, draft_id, user, db)
    assert exc_info.value.status_code == 409, exc_info.value.status_code
    assert exc_info.value.detail["error"] == "invalid_status", exc_info.value.detail
