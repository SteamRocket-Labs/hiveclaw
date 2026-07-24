from __future__ import annotations

import uuid

import pytest

from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture()
async def tenant_ids(owner_sessionmaker) -> tuple[uuid.UUID, uuid.UUID]:
    from app.models.tenant import Tenant

    first = uuid.uuid4()
    second = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=first, name="reconcile-a", slug=f"ra-{first.hex[:10]}"))
        session.add(Tenant(id=second, name="reconcile-b", slug=f"rb-{second.hex[:10]}"))
    return first, second


@pytest.fixture()
async def operator_authority(owner_sessionmaker, tenant_ids) -> tuple[uuid.UUID, uuid.UUID]:
    from app.models.agent import Agent
    from app.models.user import User

    tenant_id, _other_tenant_id = tenant_ids
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=user_id,
                username=f"reconcile-{user_id.hex[:10]}",
                email=f"{user_id.hex[:10]}@reconcile.test",
                password_hash="x",
                display_name="Reconciliation Operator",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Reconciliation Agent",
                creator_id=user_id,
            )
        )
    return user_id, agent_id


async def _add_runtime_task(
    session,
    *,
    tenant_id: uuid.UUID,
    task_type: str = "delegation",
    parent_agent_id: uuid.UUID | None = None,
    status: str = "needs_reconciliation",
    metadata: dict | None = None,
) -> RuntimeTask:
    task = RuntimeTask(
        id=uuid.uuid4(),
        task_type=task_type,
        status=status,
        tenant_id=tenant_id,
        parent_agent_id=parent_agent_id or uuid.uuid4(),
        child_agent_id=uuid.uuid4(),
        child_agent_name="worker",
        prompt="mutating work",
        result_summary="Restart interrupted a mutating run.",
        metadata_json={
            "needs_reconciliation": status == "needs_reconciliation",
            "reconciliation_reason": "missing_completion_journal",
            "side_effect_risk": "mutating",
            **(metadata or {}),
        },
    )
    session.add(task)
    await session.flush()
    return task


async def test_list_runtime_reconciliation_tasks_filters_by_tenant_and_status(owner_sessionmaker, tenant_ids):
    from app.services.runtime_reconciliation import list_runtime_reconciliation_tasks

    tenant_id, other_tenant_id = tenant_ids
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        expected = await _add_runtime_task(session, tenant_id=tenant_id)
        await _add_runtime_task(session, tenant_id=tenant_id, status="running")
        await _add_runtime_task(session, tenant_id=other_tenant_id)

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        rows = await list_runtime_reconciliation_tasks(session, tenant_id=tenant_id)

    assert [row["task_id"] for row in rows] == [str(expected.id)]
    assert rows[0]["reason"] == "missing_completion_journal"
    assert rows[0]["retry_allowed"] is False


async def test_runtime_reconciliation_retry_is_fail_closed_without_retry_contract(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    from app.services.runtime_reconciliation import RuntimeReconciliationConflict, apply_runtime_reconciliation_action

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(session, tenant_id=tenant_id, parent_agent_id=agent_id)

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeReconciliationConflict, match="not marked retryable"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                action="retry",
                reason="try again",
                actor_user_id=actor_user_id,
            )


async def test_runtime_reconciliation_safe_retry_reopens_task(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    from app.services.runtime_reconciliation import apply_runtime_reconciliation_action, get_runtime_reconciliation_task

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            parent_agent_id=agent_id,
            metadata={"reconciliation_retry_allowed": True, "side_effect_risk": "read_only"},
        )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        view = await apply_runtime_reconciliation_action(
            session,
            task_id=task.id,
            tenant_id=tenant_id,
            action="retry",
            reason="safe read-only retry",
            actor_user_id=actor_user_id,
        )

    assert view["status"] == "pending"
    assert view["metadata"]["reconciliation_status"] == "retry_requested"

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        persisted = await get_runtime_reconciliation_task(session, task_id=task.id, tenant_id=tenant_id)
    assert persisted is not None
    assert persisted["status"] == "pending"


async def test_session_bound_trigger_and_heartbeat_rows_are_actionable_without_blind_retry(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    from sqlalchemy import select

    from app.models.audit import AuditLog
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        apply_runtime_reconciliation_action,
        list_runtime_reconciliation_tasks,
    )

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        trigger = await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            task_type="trigger",
            parent_agent_id=agent_id,
            metadata={"reconciliation_reason": "session_bound_mutating_trigger"},
        )
        heartbeat = await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            task_type="heartbeat",
            parent_agent_id=agent_id,
            metadata={"reconciliation_reason": "direct_core_audit_session_bound"},
        )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        rows = await list_runtime_reconciliation_tasks(session, tenant_id=tenant_id)

    rows_by_type = {row["task_type"]: row for row in rows}
    assert rows_by_type["trigger"]["task_id"] == str(trigger.id)
    assert rows_by_type["trigger"]["reason"] == "session_bound_mutating_trigger"
    assert rows_by_type["trigger"]["retry_allowed"] is False
    assert rows_by_type["heartbeat"]["task_id"] == str(heartbeat.id)
    assert rows_by_type["heartbeat"]["reason"] == "direct_core_audit_session_bound"
    assert rows_by_type["heartbeat"]["retry_allowed"] is False

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        resolved = await apply_runtime_reconciliation_action(
            session,
            task_id=trigger.id,
            tenant_id=tenant_id,
            action="mark_resolved",
            reason="operator verified trigger side effects",
            actor_user_id=actor_user_id,
        )
        archived = await apply_runtime_reconciliation_action(
            session,
            task_id=heartbeat.id,
            tenant_id=tenant_id,
            action="archive",
            reason="operator archived interrupted heartbeat",
            actor_user_id=actor_user_id,
        )

    assert resolved["status"] == "completed"
    assert resolved["metadata"]["reconciliation_status"] == "resolved"
    assert archived["status"] == "killed"
    assert archived["metadata"]["reconciliation_status"] == "archived"

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        audit_rows = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.tenant_id == tenant_id,
                        AuditLog.action.in_(
                            (
                                "runtime_reconciliation.mark_resolved",
                                "runtime_reconciliation.archive",
                            )
                        ),
                    )
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
        with pytest.raises(RuntimeReconciliationConflict, match="no longer awaiting reconciliation"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=trigger.id,
                tenant_id=tenant_id,
                action="archive",
                reason="stale second operator action",
                actor_user_id=actor_user_id,
            )

    assert [row.action for row in audit_rows] == [
        "runtime_reconciliation.mark_resolved",
        "runtime_reconciliation.archive",
    ]
    assert audit_rows[0].details["previous_status"] == "needs_reconciliation"
    assert audit_rows[0].details["resulting_status"] == "completed"
    assert audit_rows[0].details["reconciliation_status"] == "resolved"
