"""Real-row rejection of new and already-held terminal-boundary inputs."""
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
from app.models.user import User
from app.services.runtime_terminal_boundary_outbox import enqueue_terminal_boundary
from app.services.session_input_dispatch import recover_admitted_session_inputs_once
from app.services.session_live_input import submit_live_human_input
from tests.services.test_session_input_control_v2 import _seed_session


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", ["dead_letter", "pending", "processing", "recorded_dead_letter"])
async def test_dead_letter_boundary_rejects_new_or_previously_held_message(owner_sessionmaker, initial_status):
    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    input_id = uuid4()
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        task.status = "killed"
        boundary = await enqueue_terminal_boundary(db, task=task, event_kind="turn_cancelled",
            agent_id=agent_id, session_id=session_id, terminal_status="killed", authority_ref="runtime_task",
            authority_id=task.id, binding={"terminal_event_id": uuid4(), "terminal_sequence": 1,
                "authority_sha256": "a" * 64})
        boundary.status = "pending" if initial_status == "recorded_dead_letter" else initial_status
        await db.commit()
        receipt = await submit_live_human_input(db=db, agent=await db.get(Agent, agent_id),
            user=await db.get(User, user_id), session=await db.get(ChatSession, session_id),
            content="Continue after compaction", source="r8a-dead-letter-test", input_id=input_id,
            idempotency_key=f"r8a:{input_id}")
        if initial_status != "dead_letter":
            assert receipt["dispatch"]["kind"] == "deferred"
            assert receipt["dispatch"]["code"] == "terminal_boundary_ack_pending"
            assert receipt["run"] is None
            assert (await recover_admitted_session_inputs_once(db, worker_id="r8a-input", tenant_id=tenant_id))["claimed"] == 0
            boundary.status = "dead_letter"
            if initial_status == "recorded_dead_letter":
                admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == input_id))
                admission.dispatch_receipt_json = {**admission.dispatch_receipt_json,
                    "terminal_boundary_status": "dead_letter"}
            await db.commit()
            # Uses the worker's real candidate query; changed boundary status
            # releases the hold and reaches the same rejection as a new input.
            assert await recover_admitted_session_inputs_once(db, worker_id="r8a-input", tenant_id=tenant_id) == {
                "claimed": 1, "dispatched": 1, "deferred": 0, "retried": 0}
            admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == input_id))
            dispatch = admission.dispatch_receipt_json
        else:
            dispatch = receipt["dispatch"]
        assert dispatch["kind"] == "rejected", dispatch
        assert dispatch["reason_code"] == "terminal_boundary_dead_letter"
        assert dispatch["retryable"] is False
        assert dispatch["message"] == "上一轮任务没有正常结束，这段对话暂时无法继续。请新建对话后再试。"
        row = await db.get(SessionTurnInput, input_id)
        assert row.status == "rejected"
        assert row.target_run_id is None
        assert row.content_parts_json[0]["text"] == "Continue after compaction"
        assert await db.get(RuntimeTask, uuid5(input_id, "session-v2-runtime-run")) is None
        assert (await recover_admitted_session_inputs_once(db, worker_id="r8a-input", tenant_id=tenant_id))["claimed"] == 0
