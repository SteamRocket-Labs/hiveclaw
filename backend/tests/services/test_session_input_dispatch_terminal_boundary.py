from datetime import UTC, datetime, timedelta
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
from app.models.user import User
from app.services.runtime_task_worker import _discover_terminal_boundary_tenants
from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService, enqueue_terminal_boundary
from app.services.session_input_dispatch import recover_admitted_session_inputs_once
from app.services.session_live_input import submit_live_human_input
from tests.services.test_session_input_control_v2 import _seed_session


pytestmark = pytest.mark.asyncio


async def test_discovery_holds_only_the_named_unchanged_undelivered_boundary(owner_sessionmaker):
    from app.services.session_input_dispatch import discover_admitted_session_input_locators
    from tests.services.test_session_input_control_v2 import _accepted_input, _authority

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    boundary_id = uuid4()
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        receipt = await _accepted_input(db, authority=authority, session_id=session_id)
        admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == receipt.input_id))
        admission.state, admission.dispatch_state = "admitted", "pending"
        boundary = RuntimeTerminalBoundaryOutbox(
            id=boundary_id, tenant_id=tenant_id, runtime_task_id=run_id, task_type="web_chat_turn",
            agent_id=agent_id, session_id=str(session_id), event_kind="turn_stop", terminal_status="completed",
            authority_ref="runtime_task", authority_id=str(run_id), binding_json={}, binding_sha256="a" * 64,
            idempotency_key="b" * 64, status="pending",
        )
        db.add(boundary)
        await db.commit()
        for status, receipt_status, named_id, code, boundary_session, dispatch_state, held in [
            ("pending", "pending", str(boundary_id), "terminal_boundary_ack_pending", str(session_id), "pending", True),
            ("processing", "processing", boundary_id.hex, "terminal_boundary_ack_pending", session_id.hex, "pending", True),
            ("dead_letter", "dead_letter", str(boundary_id), "terminal_boundary_ack_pending", str(session_id), "pending", True),
            ("delivered", "delivered", str(boundary_id), "terminal_boundary_ack_pending", str(session_id), "pending", False),
            ("processing", "pending", str(boundary_id), "terminal_boundary_ack_pending", str(session_id), "pending", False),
            ("pending", "pending", str(uuid4()), "terminal_boundary_ack_pending", str(session_id), "pending", False),
            ("pending", "missing", None, "terminal_boundary_ack_pending", str(session_id), "pending", False),
            ("pending", "pending", "invalid-id", "terminal_boundary_ack_pending", str(session_id), "pending", False),
            ("pending", "pending", str(boundary_id).upper(), "terminal_boundary_ack_pending", str(session_id), "pending", False),
            ("pending", "pending", str(boundary_id), "other", str(session_id), "pending", False),
            ("pending", "pending", str(boundary_id), "terminal_boundary_ack_pending", str(uuid4()), "pending", False),
            ("pending", "pending", str(boundary_id), "terminal_boundary_ack_pending", str(session_id), "dispatching", False),
        ]:
            boundary.status, boundary.session_id = status, boundary_session
            admission.dispatch_state = dispatch_state
            admission.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            admission.dispatch_receipt_json = {
                "code": code, "terminal_boundary_id": named_id, "terminal_boundary_status": receipt_status,
            }
            await db.commit()
            found = await discover_admitted_session_input_locators(db, tenant_id=tenant_id)
            assert found == ([] if held else [(tenant_id, admission.id)])

        # A boundary in another tenant cannot hold this input even with an exact ID.
        other_tenant, _, _, _, _ = await _seed_session(owner_sessionmaker)
        boundary.tenant_id, boundary.session_id = other_tenant, str(session_id)
        admission.dispatch_state = "pending"
        await db.commit()
        assert await discover_admitted_session_input_locators(db, tenant_id=tenant_id) == [(tenant_id, admission.id)]


@pytest.mark.parametrize("missing_boundary", [False, True], ids=["old-pending", "old-missing"])
async def test_old_web_boundary_delivery_releases_next_session_input(owner_sessionmaker, missing_boundary):
    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    old = datetime.now(UTC) - timedelta(hours=25)
    next_input_id = uuid4()

    async def build(db, task):
        row = await enqueue_terminal_boundary(db, task=task, event_kind="turn_stop", agent_id=agent_id,
            session_id=session_id, terminal_status="completed", authority_ref="runtime_task", authority_id=task.id,
            binding={"terminal_event_id": uuid4(), "terminal_sequence": 1, "authority_sha256": "a" * 64})
        return (row,)

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        task.status = "completed"
        task.created_at = task.completed_at = old
        if not missing_boundary:
            row, = await build(db, task)
            row.available_at = row.created_at = old
        await db.commit()
        receipt = await submit_live_human_input(db=db, agent=await db.get(Agent, agent_id),
            user=await db.get(User, user_id), session=await db.get(ChatSession, session_id),
            content="Continue after the previous result", source="old-web-boundary-test",
            input_id=next_input_id, idempotency_key=f"old-web:{next_input_id}")
        assert receipt["dispatch"]["kind"] == "deferred"
        assert receipt["dispatch"]["status"] == (
            "waiting_for_terminal_boundary_reconciliation" if missing_boundary else "waiting_for_terminal_boundary_ack")

    assert tenant_id in await _discover_terminal_boundary_tenants(worker_id="old-web", task_types=("web_chat_turn",),
        session_factory=owner_sessionmaker, limit=1000)
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)
    if missing_boundary:
        assert await service.reconcile_terminal_tasks_once(tenant_id=tenant_id, builder=build,
            task_types=("web_chat_turn",)) == {"claimed": 1, "enqueued": 1, "held": 0}
    claimed = await service.claim_batch(tenant_id=tenant_id, worker_id="old-web", task_types=("web_chat_turn",))
    assert [item.runtime_task_id for item in claimed] == [run_id]

    async def canonical(_db, item):
        return dict(item.binding)

    async def deliver(item):
        return {"source_ref": f"runtime-terminal-boundary://{item.id}"}

    assert await service.process_terminal_boundary(item=claimed[0], worker_id="old-web",
        canonical_validator=canonical, process_callback=deliver)
    async with owner_sessionmaker() as db:
        boundary = await db.get(RuntimeTerminalBoundaryOutbox, claimed[0].id)
        assert boundary.status == "delivered"
        recovery = await recover_admitted_session_inputs_once(db, worker_id="old-web-input", tenant_id=tenant_id)
        assert recovery == {"claimed": 1, "dispatched": 1, "deferred": 0, "retried": 0}
        admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == next_input_id))
        assert admission.dispatch_state == "dispatched"
        next_input = await db.get(SessionTurnInput, next_input_id)
        assert next_input.target_run_id == uuid5(next_input_id, "session-v2-runtime-run")
        assert await db.get(RuntimeTask, next_input.target_run_id) is not None
