"""Maintained regression: interrupted claim-quarantine recovery attribution.

Eighth correction (CC7 F5): when ``_finish_deferred_business_task_quarantines``
is interrupted right after the claim transaction commits, the committed
``needs_reconciliation`` business task is recovered by the DIRECT
terminal-boundary lane (``drain_direct_terminal_boundary_outbox_once`` →
``RuntimeTerminalBoundaryOutboxService.reconcile_terminal_tasks_once``), NOT
by the worker-restart orphan sweep (its predicate is ``running`` only) and
not directly by the operator action (which is rejected with
``terminal_projection_missing`` until the lane repairs the boundary). After
the direct lane delivers, the operator reconciliation action consumes the
recovered boundary end-to-end.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed_business_task_with_broken_link(sessionmaker):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User

    suffix = uuid.uuid4().hex[:10]
    async with sessionmaker() as db:
        tenant = Tenant(name="Eighth Quarantine Tenant", slug=f"e8q-{suffix}", is_active=True)
        db.add(tenant)
        await db.flush()
        user = User(
            username=f"e8q-{suffix}",
            email=f"e8q-{suffix}@example.test",
            password_hash="x",
            display_name="Eighth Quarantine User",
            tenant_id=tenant.id,
            role="org_admin",
        )
        db.add(user)
        await db.flush()
        agent = Agent(name="Eighth Q Agent", creator_id=user.id, owner_user_id=user.id, tenant_id=tenant.id)
        db.add(agent)
        await db.flush()
        session = ChatSession(agent_id=agent.id, tenant_id=tenant.id, user_id=user.id)
        db.add(session)
        await db.flush()
        task = RuntimeTask(
            task_type="business_task",
            status="pending",
            tenant_id=tenant.id,
            parent_agent_id=agent.id,
            parent_session_id=str(session.id),
            root_user_id=user.id,
            prompt="eighth broken projection link",
            metadata_json={"business_task_id": str(uuid.uuid4())},
        )
        db.add(task)
        await db.commit()
        return tenant.id, user.id, task.id


async def test_interrupted_quarantine_recovers_via_direct_lane_then_operator(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, drain_terminal_boundary_for_task
):
    import app.services.runtime_task_claim_service as claim_service
    from app.database import tenant_scoped_session
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_worker import drain_direct_terminal_boundary_outbox_once

    tenant_id, user_id, task_id = await _seed_business_task_with_broken_link(owner_sessionmaker)

    # Interrupt EXACTLY after the claim transaction commits.
    async def _crash(task_ids):
        raise RuntimeError("eighth: process interrupted right after the claim commit")

    monkeypatch.setattr(claim_service, "_finish_deferred_business_task_quarantines", _crash)
    with pytest.raises(RuntimeError):
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            await RuntimeTaskClaimService(
                db=db, worker_id="eighth-worker", task_types=("business_task",)
            ).claim_available(batch_size=5)

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, task_id)
        assert task.status == "needs_reconciliation"
        assert task.terminal_boundary_generation is not None
        assert task.terminal_boundary_enqueued_at is None
        interrupted_shape = {
            "fence_ref": (task.metadata_json or {}).get("terminal_execution_fence_ref"),
            "root_runtime_task_id": task.root_runtime_task_id,
        }

    # The worker-restart orphan sweep does NOT cover this state (CC7 F5).
    from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks

    await reconcile_orphaned_runtime_tasks()
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, task_id)
        assert task.status == "needs_reconciliation"

    # The real consumer: the direct terminal-boundary lane repairs the
    # boundary, and the operator action then consumes it end-to-end.
    counts = await drain_terminal_boundary_for_task(
        drain_direct_terminal_boundary_outbox_once, task_id=task_id, worker_id="eighth-direct-lane"
    )
    assert counts["delivered"] >= 1, counts

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, task_id)
        assert task.terminal_boundary_enqueued_at is not None, counts
        # Documented shape difference vs the happy path: the interrupted
        # lifecycle settled no terminal fence metadata (only the later
        # operator transition stamps its own fence). For this business task
        # the root ledger is not applicable (no root_runtime_task_id).
        assert (task.metadata_json or {}).get("terminal_execution_fence_ref") == interrupted_shape["fence_ref"]
        assert task.root_runtime_task_id is interrupted_shape["root_runtime_task_id"] is None

    from app.services.runtime_reconciliation import apply_runtime_reconciliation_action

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        result = await apply_runtime_reconciliation_action(
            db,
            task_id=task_id,
            tenant_id=tenant_id,
            action="mark_resolved",
            reason="eighth operator resolves the lane-recovered quarantine",
            actor_user_id=user_id,
        )
        await db.commit()
    assert result.get("status") == "completed", result

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, task_id)
        metadata = dict(task.metadata_json or {})
        assert task.status == "completed"
        assert metadata.get("terminal_execution_fence_ref"), (
            f"the operator's terminal transition must stamp its own fence: {metadata}"
        )
        assert metadata.get("terminal_committed_status") == "completed", metadata
