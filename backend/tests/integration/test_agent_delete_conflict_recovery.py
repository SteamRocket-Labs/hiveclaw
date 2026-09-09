"""Maintained regressions for typed batch-conflict recovery on Agent deletion.

Seventh correction (CC6 B5): a ``RuntimeTaskLateAdmissionConflict`` /
``RuntimeTaskSessionBindingConflict`` raised by ``soft_delete_agent`` must
surface as a truthful retryable 409 with the transaction rolled back and NO
destructive cleanup consumed; the destructive file archival runs only after
the commit, and a failed archival surfaces a truthful partial state whose
supported retry is the SAME DELETE request (cleanup-only once the deletion
committed). The HR abandon flow shares the shape, and the claim-time
business-task quarantine settles its terminal boundary in its own
advisory-first transaction.

Eighth correction (CC7 F1/F2/F3): the cleanup-only retry must be reachable
through the REAL authority gates (no stubbed ``check_agent_access``) via
``load_deleted_agent_for_cleanup``, must never return success on a hidden
archival failure, and the HR abandon route must map the typed batch
conflicts and expose the same repeat-request cleanup retry.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
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


def _endpoint_harness(monkeypatch, *, deleted_at, cleanup_outcome) -> tuple[list[str], _FakeDB]:
    import app.api.agents as api
    import app.core.permissions as permissions

    effects: list[str] = []
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="Probe Agent",
        metadata_json={},
        deleted_at=deleted_at,
    )
    actor = SimpleNamespace(id=uuid.uuid4(), role="org_admin")
    db = _FakeDB()

    async def access(*args, **kwargs):
        return agent, None

    async def cleanup_lookup(*args, **kwargs):
        # Unit-seam stand-in for the real cleanup-only loader: it resolves
        # exactly the already-deleted Agent and nothing else.
        return agent if deleted_at is not None else None

    async def soft_delete(*args, **kwargs):
        effects.append("soft_delete")

    async def perform(*args, **kwargs):
        effects.append("perform_cleanup")
        return cleanup_outcome

    import app.core.policy as policy
    import app.services.agent_identity_lifecycle as lifecycle
    import app.services.ai_assets as ai_assets

    async def projection(*args, **kwargs):
        effects.append("register_projection")

    async def audit(*args, **kwargs):
        effects.append("audit")

    monkeypatch.setattr(permissions, "load_deleted_agent_for_cleanup", cleanup_lookup)
    monkeypatch.setattr(api, "check_agent_access", access)
    monkeypatch.setattr(api, "soft_delete_agent", soft_delete)
    monkeypatch.setattr(lifecycle, "perform_pending_agent_cleanup", perform)
    monkeypatch.setattr(ai_assets, "register_projection", projection)
    monkeypatch.setattr(policy, "write_audit_event", audit)
    return effects, db, agent, actor


async def test_late_admission_conflict_maps_to_retryable_409_without_cleanup(monkeypatch):
    import app.api.agents as api
    from app.services.runtime_terminal_settlement import RuntimeTaskLateAdmissionConflict

    effects, db, agent, actor = _endpoint_harness(
        monkeypatch, deleted_at=None, cleanup_outcome={"performed": True, "archive_ok": True, "marker_cleared": True}
    )

    async def conflict(*args, **kwargs):
        raise RuntimeTaskLateAdmissionConflict("injected sustained admission pressure")

    monkeypatch.setattr(api, "soft_delete_agent", conflict)
    with pytest.raises(api.HTTPException) as exc_info:
        await api.delete_agent(agent.id, current_user=actor, db=db)
    assert exc_info.value.status_code == 409, exc_info.value.status_code
    detail = exc_info.value.detail
    assert detail["code"] == "agent_delete_late_admission_conflict", detail
    assert detail["retryable"] is True, detail
    assert "rollback" in db.calls and "commit" not in db.calls, db.calls
    assert effects == [], f"destructive or external cleanup ran before the rollback: {effects}"


async def test_session_binding_conflict_is_classified_not_promise_of_retry(monkeypatch):
    import app.api.agents as api
    from app.services.runtime_terminal_settlement import RuntimeTaskSessionBindingConflict

    effects, db, agent, actor = _endpoint_harness(
        monkeypatch, deleted_at=None, cleanup_outcome={"performed": True, "archive_ok": True, "marker_cleared": True}
    )

    async def conflict(*args, **kwargs):
        raise RuntimeTaskSessionBindingConflict("injected impossible binding change")

    monkeypatch.setattr(api, "soft_delete_agent", conflict)
    with pytest.raises(api.HTTPException) as exc_info:
        await api.delete_agent(agent.id, current_user=actor, db=db)
    detail = exc_info.value.detail
    assert detail["code"] == "agent_delete_session_binding_conflict", detail
    assert detail["retryable"] is False, (
        "an impossible binding change must not be reported with an identical-retry promise"
    )
    assert effects == [], effects


async def test_cleanup_failure_after_commit_is_a_truthful_partial_state(monkeypatch):
    """A committed deletion with failed archival must not return 204 success."""

    import app.api.agents as api

    effects, db, agent, actor = _endpoint_harness(
        monkeypatch,
        deleted_at=None,
        cleanup_outcome={"performed": True, "archive_ok": False},
    )
    with pytest.raises(api.HTTPException) as exc_info:
        await api.delete_agent(agent.id, current_user=actor, db=db)
    assert exc_info.value.status_code == 503, exc_info.value.status_code
    detail = exc_info.value.detail
    assert detail["code"] == "agent_cleanup_pending", detail
    assert detail["retryable"] is True, detail
    assert "committed" in detail["message"] and "NOT rolled back" in detail["message"], detail
    assert db.calls == ["commit"], db.calls
    assert effects.index("soft_delete") < effects.index("perform_cleanup"), effects


async def test_redelete_of_deleted_agent_is_the_supported_cleanup_retry(monkeypatch):
    import app.api.agents as api

    effects, db, agent, actor = _endpoint_harness(
        monkeypatch, deleted_at=object(), cleanup_outcome={"performed": True, "archive_ok": True}
    )
    assert await api.delete_agent(agent.id, current_user=actor, db=db) is None
    assert effects == ["perform_cleanup"], effects
    assert db.calls == [], "the retry path must not re-run the deletion transaction"


async def test_redelete_with_still_failing_cleanup_keeps_the_partial_state(monkeypatch):
    import app.api.agents as api

    effects, db, agent, actor = _endpoint_harness(
        monkeypatch, deleted_at=object(), cleanup_outcome={"performed": True, "archive_ok": False}
    )
    with pytest.raises(api.HTTPException) as exc_info:
        await api.delete_agent(agent.id, current_user=actor, db=db)
    assert exc_info.value.status_code == 503, exc_info.value.status_code
    assert exc_info.value.detail["code"] == "agent_cleanup_pending", exc_info.value.detail
    assert effects == ["perform_cleanup"], effects


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


async def test_redelete_of_deleted_agent_through_the_real_authority_gate(owner_sessionmaker, monkeypatch):
    """CC7 F1: the cleanup-only retry is reachable with the REAL permission gates.

    No stub for ``check_agent_access`` or the cleanup loader: a same-tenant
    org administrator re-issuing DELETE for a committed soft-deleted Agent
    must reach the cleanup-only branch; a foreign-tenant administrator must
    get the ordinary 404 with ZERO cleanup calls; and a successful retry
    must not duplicate any lifecycle or audit effect.
    """

    import app.api.agents as api
    import app.services.agent_identity_lifecycle as lifecycle
    from app.models.agent import Agent
    from app.models.security_audit import SecurityAuditEvent
    from app.models.user import User

    tenant_id, agent_id, admin_id = await _seed_admin_agent(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        agent.deleted_at = datetime.now(UTC)
        await db.commit()

    cleanup_calls: list[uuid.UUID] = []
    original = lifecycle.perform_pending_agent_cleanup

    async def _recording(tenant, aid):
        cleanup_calls.append(aid)
        return await original(tenant, aid)

    monkeypatch.setattr(lifecycle, "perform_pending_agent_cleanup", _recording)
    import app.services.agent_manager as agent_manager

    async def _archive_ok(agent_uuid):
        return None

    monkeypatch.setattr(agent_manager.agent_manager, "archive_agent_files", _archive_ok)

    async with owner_sessionmaker() as db:
        admin = await db.get(User, admin_id)
        assert await api.delete_agent(agent_id, current_user=admin, db=db) is None
    assert cleanup_calls == [agent_id], cleanup_calls

    async with owner_sessionmaker() as db:
        audit_count = len(
            (await db.execute(select(SecurityAuditEvent).where(SecurityAuditEvent.resource_id == agent_id)))
            .scalars()
            .all()
        )
    assert audit_count == 0, "the cleanup-only retry must not write new audit events"

    # Same-request retry semantics: a second re-delete after success is a
    # harmless no-op (nothing left to clean), still through the real gate.
    async with owner_sessionmaker() as db:
        admin = await db.get(User, admin_id)
        assert await api.delete_agent(agent_id, current_user=admin, db=db) is None
    assert len(cleanup_calls) == 2, cleanup_calls


async def test_redelete_after_failed_archival_returns_partial_state_then_recovers(owner_sessionmaker, monkeypatch):
    """Committed deletion + failed archival: 503 partial truth, then the same
    request retries the cleanup only and succeeds."""

    import app.api.agents as api
    from app.models.agent import Agent
    from app.models.user import User

    tenant_id, agent_id, admin_id = await _seed_admin_agent(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        agent.deleted_at = datetime.now(UTC)
        await db.commit()

    import app.services.agent_manager as agent_manager

    calls: list[uuid.UUID] = []

    async def _flaky(agent_uuid):
        calls.append(agent_uuid)
        if len(calls) == 1:
            raise RuntimeError("maintained: archival unavailable")

    monkeypatch.setattr(agent_manager.agent_manager, "archive_agent_files", _flaky)

    async with owner_sessionmaker() as db:
        admin = await db.get(User, admin_id)
        with pytest.raises(api.HTTPException) as exc_info:
            await api.delete_agent(agent_id, current_user=admin, db=db)
    assert exc_info.value.status_code == 503, exc_info.value.status_code
    assert exc_info.value.detail["code"] == "agent_cleanup_pending", exc_info.value.detail

    async with owner_sessionmaker() as db:
        admin = await db.get(User, admin_id)
        assert await api.delete_agent(agent_id, current_user=admin, db=db) is None
    assert len(calls) == 2, calls


async def test_redelete_by_foreign_tenant_admin_is_a_clean_404(owner_sessionmaker, monkeypatch):
    """Tenant isolation holds on the cleanup-only path: zero cleanup calls."""

    import app.api.agents as api
    import app.services.agent_identity_lifecycle as lifecycle
    from app.models.user import User

    tenant_id, agent_id, foreign_admin_id = await _seed_admin_agent(owner_sessionmaker, shared_tenant=False)
    import app.services.agent_manager as agent_manager

    async def _archive_spy(agent_uuid):
        raise AssertionError("foreign-tenant cleanup must never run")

    monkeypatch.setattr(agent_manager.agent_manager, "archive_agent_files", _archive_spy)
    cleanup_calls: list[uuid.UUID] = []
    original = lifecycle.perform_pending_agent_cleanup

    async def _recording(tenant, aid):
        cleanup_calls.append(aid)
        return await original(tenant, aid)

    monkeypatch.setattr(lifecycle, "perform_pending_agent_cleanup", _recording)

    async with owner_sessionmaker() as db:
        admin = await db.get(User, foreign_admin_id)
        with pytest.raises(api.HTTPException) as exc_info:
            await api.delete_agent(agent_id, current_user=admin, db=db)
    assert exc_info.value.status_code == 404, exc_info.value.status_code
    assert cleanup_calls == [], cleanup_calls


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
