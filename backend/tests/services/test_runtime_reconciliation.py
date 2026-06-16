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


async def _add_runtime_task(
    session,
    *,
    tenant_id: uuid.UUID,
    status: str = "needs_reconciliation",
    metadata: dict | None = None,
) -> RuntimeTask:
    task = RuntimeTask(
        id=uuid.uuid4(),
        task_type="delegation",
        status=status,
        tenant_id=tenant_id,
        parent_agent_id=uuid.uuid4(),
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


async def test_runtime_reconciliation_retry_is_fail_closed_without_retry_contract(owner_sessionmaker, tenant_ids):
    from app.services.runtime_reconciliation import RuntimeReconciliationConflict, apply_runtime_reconciliation_action

    tenant_id, _other = tenant_ids
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(session, tenant_id=tenant_id)

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeReconciliationConflict, match="not marked retryable"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                action="retry",
                reason="try again",
                actor_user_id=uuid.uuid4(),
            )


async def test_runtime_reconciliation_safe_retry_reopens_task(owner_sessionmaker, tenant_ids):
    from app.services.runtime_reconciliation import apply_runtime_reconciliation_action, get_runtime_reconciliation_task

    tenant_id, _other = tenant_ids
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            metadata={"reconciliation_retry_allowed": True, "side_effect_risk": "read_only"},
        )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        view = await apply_runtime_reconciliation_action(
            session,
            task_id=task.id,
            tenant_id=tenant_id,
            action="retry",
            reason="safe read-only retry",
            actor_user_id=uuid.uuid4(),
        )

    assert view["status"] == "pending"
    assert view["metadata"]["reconciliation_status"] == "retry_requested"

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        persisted = await get_runtime_reconciliation_task(session, task_id=task.id, tenant_id=tenant_id)
    assert persisted is not None
    assert persisted["status"] == "pending"
