"""Actionable routine results stay with their intended Web recipient."""

import asyncio
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.integration.test_m1g7_routine_settings_postgres import scoped_runtime_factories  # noqa: F401
from tests.integration.test_m3ag_native_trigger_questions_postgres import scheduled_question_runtime, submit_answer  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("online", [True, False])
async def test_actionable_routine_delivers_only_to_its_original_web_work(scheduled_question_runtime, monkeypatch, online):  # noqa: F811
    import app.runtime.invoker as invoker
    from app.api import websocket
    from app.models.chat_session import ChatSession
    from app.models.capability_policy import CapabilityPolicy
    from app.models.session_v2 import SessionToolInvocation
    from app.models.audit import ApprovalRequest, ChatMessage
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.services import runtime_task_worker, trigger_daemon, web_chat_runtime
    from app.services.web_chat_broker import WebChatBroker

    state = scheduled_question_runtime
    await submit_answer(state, state.question.item_id, "Audience: Team")
    successor = await state.claim("web_chat_turn")
    state.llm.script.append({"content": "The original audience decision is recorded.", "finish_reason": "stop"})
    await web_chat_runtime.execute_web_chat_run(successor)
    await state.boundary(successor)
    async with state.scoped() as db:
        db.add(CapabilityPolicy(tenant_id=state.seed["tenant_id"], agent_id=state.seed["agent_id"],
            capability="channel.message.send", allowed=True, requires_approval=False))
        user = await db.get(User, state.seed["user_id"])
        username = user.username
        other = User(id=uuid.uuid4(), tenant_id=state.seed["tenant_id"], username=uuid.uuid4().hex,
            email=uuid.uuid4().hex + "@recipient.test", display_name="Other colleague", password_hash="test-only")
        db.add(other)
        await db.flush()
        other_session = ChatSession(id=uuid.uuid4(), tenant_id=state.seed["tenant_id"], agent_id=state.seed["agent_id"],
            user_id=other.id, source_channel="web", audience_kind="private")
        db.add(other_session)
        other_session_id = other_session.id
        await db.commit()

    class Socket:
        def __init__(self):
            self.events = []

        async def accept(self):
            pass

        async def send_json(self, payload):
            self.events.append(payload)

    broker = WebChatBroker()
    recipient, bystander, unbound = Socket(), Socket(), Socket()
    if online:
        await broker.connect(str(state.seed["agent_id"]), recipient, str(state.seed["session_id"]))
    await broker.connect(str(state.seed["agent_id"]), bystander, str(other_session_id))
    await broker.connect(str(state.seed["agent_id"]), unbound)
    monkeypatch.setattr(websocket, "manager", broker)
    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", lambda *a, **k: [{"type": "function", "function": {
        "name": "send_web_message", "parameters": {"type": "object", "properties": {
            "username": {"type": "string"}, "message": {"type": "string"}}, "required": ["username", "message"]}}}])
    marker = "ROUTINE_ACTION_REQUIRED: current stock is below the agreed minimum; review the replenishment proposal in this work."
    async with AsyncClient(transport=ASGITransport(app=state.app), base_url="http://actionable-routine.test") as client:
        draft = await client.post(state.endpoint + "/triggers", json={"name": "actionable_stock_check", "type": "interval",
            "config": {"minutes": 30, "delivery": "same_session", "source_session_id": str(state.seed["session_id"]),
                "model_id": str(state.pinned_id), "toolset": ["send_web_message"]},
            "reason": "When the agreed stock check needs attention, notify me in this original Web work. Keep nonactionable checks quiet.",
            "is_enabled": False})
        assert draft.status_code == 201, draft.text
        trigger_id = draft.json()["id"]
        review = await client.post(state.endpoint + "/plan-recommendations", json={"session_id": str(state.seed["session_id"]),
            "original_request": "Enable the routine notification within its existing authority.", "source": "agent_aware",
            "action_kind": "enable_autonomous_wake", "tool_name": "trigger_rest"})
        assert review.status_code == 201, review.text
        declined = await client.post(state.endpoint + "/plan-recommendations/" + review.json()["id"] + "/decline")
        assert declined.status_code == 200, declined.text
        enabled = await client.patch(state.endpoint + "/triggers/" + trigger_id, json={"is_enabled": True,
            "plan_mode_decision": "declined", "plan_recommendation_id": declined.json()["id"]})
        assert enabled.status_code == 200, enabled.text
        state.llm.script.extend([
            {"tool_calls": [{"id": "notify-stock-owner", "type": "function", "function": {
                "name": "send_web_message", "arguments": json.dumps({"username": username, "message": marker})}}], "finish_reason": "tool_calls"},
            {"content": "The replenishment proposal is recorded in this work.", "finish_reason": "stop"},
        ])
        fired = await trigger_daemon.fire_trigger_once_now(state.seed["agent_id"], trigger_id)
        assert fired["fired"], fired
        wrapper_id = await state.claim("trigger")
        assert await trigger_daemon.execute_claimed_trigger_runtime_task(str(wrapper_id))
        await runtime_task_worker.drain_direct_terminal_boundary_outbox_once(worker_id="actionable-wrapper", session_factory=state.scoped)
        await asyncio.sleep(5.1)
        await runtime_task_worker.recover_session_input_dispatches_once(worker_id="actionable-input", session_factory=state.scoped)
        run_id = await state.claim("web_chat_turn")
        await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(run_id), 20)
        async with state.scoped() as db:
            run = await db.get(RuntimeTask, run_id)
            assert run.status == "completed", run.result_summary
        async with state.scoped() as db:
            ticket = (await db.scalars(select(ApprovalRequest).where(ApprovalRequest.agent_id == state.seed["agent_id"],
                ApprovalRequest.tool_name == "send_web_message"))).one()
            assert ticket.status == "pending" and ticket.consumed_at is None
            ticket_id = ticket.id
        assert recipient.events == bystander.events == unbound.events == []
        approval_url = state.endpoint + f"/sessions/{state.seed['session_id']}/approvals/{ticket_id}/resolve"
        approved = await client.post(approval_url, json={"action": "approve"})
        assert approved.status_code == 200 and approved.json()["execution_status"] == "queued", approved.text
        from app.services.approval_execution_runtime import execute_claimed_approval_execution
        from app.services.runtime_task_claim_service import RuntimeTaskClaimService
        from app.services.runtime_task_fence import run_claimed_runtime_task

        async with state.scoped() as db:
            job = (await RuntimeTaskClaimService(db=db, worker_id="actionable-approval", task_types=["approval_execution"],
                lease_seconds=180).claim_available(batch_size=1))[0]
            job_id, version = job.id, job.claim_version
        outcome = await run_claimed_runtime_task(execute_claimed_approval_execution(job_id), task_id=job_id,
            claim_version=version, worker_id="actionable-approval", lease_seconds=180)
        assert outcome == "succeeded", outcome
        assert bool([event for event in recipient.events if event.get("content") == marker]) is online, recipient.events
        assert bystander.events == [] and unbound.events == [], "A routine must never broadcast private results to other work"
        async with state.scoped() as db:
            invocation = (await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == run_id,
                SessionToolInvocation.tool_name == "send_web_message"))).one()
            assert invocation.effect_state == "prepared_not_started" and invocation.result_event_id is not None
            ticket = await db.get(ApprovalRequest, ticket_id)
            assert ticket.execution_status == "succeeded" and ticket.consumed_at is not None
            assert ticket.normalized_arguments == {"username": username, "message": marker}
            assert invocation.effective_arguments_json == {"username": username, "message": marker}
            sent = list(await db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == str(state.seed["session_id"]),
                ChatMessage.content == marker)))
            assert len(sent) == 1
        history = await client.get(state.endpoint + f"/sessions/{state.seed['session_id']}/messages")
        assert history.status_code == 200 and marker in history.text, history.text
        assert await execute_claimed_approval_execution(job_id) == "succeeded"
        assert len(recipient.events) == int(online) and bystander.events == unbound.events == []
        await state.boundary(run_id)
        before = len(state.llm.requests)
        state.llm.script.clear()
        state.llm.script.append({"content": "No additional action is needed; the current proposal remains in this work.", "finish_reason": "stop"})
        second = await trigger_daemon.fire_trigger_once_now(state.seed["agent_id"], trigger_id)
        assert second["fired"], second
        second_wrapper = await state.claim("trigger")
        assert await trigger_daemon.execute_claimed_trigger_runtime_task(str(second_wrapper))
        await runtime_task_worker.drain_direct_terminal_boundary_outbox_once(worker_id="next-wrapper", session_factory=state.scoped)
        await asyncio.sleep(5.1)
        await runtime_task_worker.recover_session_input_dispatches_once(worker_id="next-input", session_factory=state.scoped)
        next_run = await state.claim("web_chat_turn")
        await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(next_run), 20)
        assert len(state.llm.requests) == before + 1
        assert marker in json.dumps(state.llm.requests[-1]["messages"]), "The subsequent real provider request must consume the recorded proposal"
        async with state.scoped() as db:
            assert (await db.get(RuntimeTask, next_run)).status == "completed"
        assert len(recipient.events) == int(online) and bystander.events == unbound.events == []
