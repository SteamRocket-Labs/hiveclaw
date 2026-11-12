"""A later completed parent turn already consumed the delayed child result."""

from uuid import UUID

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from tests.integration.test_failed_delegation_continuation import _call, _execute, _failed_child
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient


@pytest.fixture
def auxiliary_models(monkeypatch):
    class AuxiliaryClient(ScriptedLLMClient):
        async def complete(self, **kwargs):
            return await self.stream(**kwargs)

    def create(*_args, **_kwargs):
        return AuxiliaryClient([{"content": "{}"}])

    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", create)
    monkeypatch.setattr("app.services.fast_reflection_learning_brain.create_llm_client_from_config", create)


@pytest.mark.parametrize("reader", ["check_async_task", "task_output", "list_async_tasks", "read_runtime_result"])
@pytest.mark.parametrize("budget_waiting", [False, True])
async def test_later_parent_read_settles_notification_without_a_new_model_turn(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, reader, auxiliary_models, budget_waiting,
):
    import app.runtime.invoker as invoker
    from app.services.web_chat_runtime import start_web_chat_run

    async def read_before_notification(seed):
        async with owner_sessionmaker() as db:
            notification = await db.scalar(select(RuntimeNotificationOutbox).where(
                RuntimeNotificationOutbox.source_run_id == str(seed["child_id"])))
            seed["result_ref"] = notification.result_ref
            session = await db.get(ChatSession, seed["session_id"])
            next_run = await start_web_chat_run(db=db, agent=await db.get(Agent, seed["agent_id"]),
                user=await db.get(User, seed["user_id"]), session=session,
                content="Read the colleague result and summarize it now.", extra_metadata={"source": "websocket_message"})
            seed["later_run_id"] = UUID(next_run["run_id"])
            await db.commit()
        arguments = {"check_async_task": {"task_id": str(seed["child_id"])},
            "task_output": {"runtime_task_id": str(seed["child_id"])},
            "list_async_tasks": {},
            "read_runtime_result": {"result_ref": seed["result_ref"]}}[reader]
        client = ScriptedLLMClient([{"tool_calls": [_call(reader, arguments, "already-read")],
            "finish_reason": "tool_calls"}, {"content": "The three points are quality, service, and value."}])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
        await _execute(owner_sessionmaker, seed["later_run_id"], "r19a-later-parent")
        assert len(client.requests) == 2
        async with owner_sessionmaker() as db:
            later = await db.get(RuntimeTask, seed["later_run_id"])
            assert later.status == "completed", later.result_summary
            events = list(await db.scalars(select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.run_id == later.id, ChatTranscriptEvent.item_kind == "tool_result")))
            assert len(events) == 1
            assert "quality" in events[0].metadata_json["v2_payload"]["content"]
        if budget_waiting:
            from types import SimpleNamespace

            async def wait_for_budget(*_args, **_kwargs):
                return SimpleNamespace(waiting=True)

            monkeypatch.setattr("app.services.runtime_notification_outbox.ExecutionAdmission.admit", wait_for_budget)

    seed, _ = await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        child_success=True, before_notification=read_before_notification)
    assert seed["return_id"] is None, "A completed later read must not queue another model turn"
    async with owner_sessionmaker() as db:
        notification = await db.scalar(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == str(seed["child_id"])))
        assert notification.status == "delivered" and notification.attempt_count == 1
        child = await db.get(RuntimeTask, seed["child_id"])
        assert child.completion_outbox_settled_at is not None
        receipts = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == seed["session_id"],
            ChatTranscriptEvent.event_type == "agent_task_notification")))
        assert len(receipts) == 1


async def test_a_running_status_read_does_not_consume_the_later_result(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, auxiliary_models,
):
    import app.runtime.invoker as invoker
    from app.services.web_chat_runtime import start_web_chat_run

    async def read_running(seed):
        async with owner_sessionmaker() as db:
            queued = await start_web_chat_run(db=db, agent=await db.get(Agent, seed["agent_id"]),
                user=await db.get(User, seed["user_id"]), session=await db.get(ChatSession, seed["session_id"]),
                content="Check whether the colleague has finished.", extra_metadata={"source": "websocket_message"})
            run_id = UUID(queued["run_id"])
            await db.commit()
        client = ScriptedLLMClient([{"tool_calls": [_call("check_async_task", {"task_id": str(seed["child_id"])},
            "running-read")], "finish_reason": "tool_calls"}, {"content": "The colleague is still working."}])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
        await _execute(owner_sessionmaker, run_id, "r19a-running-read")
        assert len(client.requests) == 2
        output = next(message["content"] for message in client.requests[1]["messages"]
            if message.get("tool_call_id") == "running-read")
        import json

        assert json.loads(output)["status"] == "running"

    seed, _ = await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        child_success=True, before_child_completion=read_running)
    assert seed["return_id"] is not None, "A running read does not consume the finished result"


async def test_backend_emits_one_receipt_for_one_child_even_when_delivery_replays(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, auxiliary_models,
):
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService

    seed, _ = await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, child_success=True)
    service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
    receipt = await service._deliver_page(seed["delivery_page"])
    assert receipt["deduplicated"] is True
    await service._mark_page_delivered(page=seed["delivery_page"], worker_id="r13b-return", receipt=receipt)
    async with owner_sessionmaker() as db:
        events = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == seed["session_id"])))
        notifications = [event for event in events if event.event_type == "agent_task_notification"]
        assert len(notifications) == 1
        assert notifications[0].metadata_json["item_count"] == 1
        print("R19A_BACKEND_RESULT_RECEIPTS", [(event.event_type, event.sequence) for event in notifications])


async def test_failed_parent_turn_that_read_the_result_does_not_repeat_it(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, auxiliary_models,
):
    import app.runtime.invoker as invoker
    from app.kernel.loop_guard import LoopGuard
    from app.services.web_chat_runtime import start_web_chat_run

    async def read_until_guard(seed):
        async with owner_sessionmaker() as db:
            queued = await start_web_chat_run(db=db, agent=await db.get(Agent, seed["agent_id"]),
                user=await db.get(User, seed["user_id"]), session=await db.get(ChatSession, seed["session_id"]),
                content="Read the colleague's finished task.", extra_metadata={"source": "websocket_message"})
            run_id = UUID(queued["run_id"])
            await db.commit()
        client = ScriptedLLMClient([{"tool_calls": [_call("check_async_task", {"task_id": str(seed["child_id"])},
            f"terminal-read-{i}")], "finish_reason": "tool_calls"} for i in range(LoopGuard().identical_tool_threshold)])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
        await _execute(owner_sessionmaker, run_id, "r19a-failed-later-parent")
        assert len(client.requests) == LoopGuard().identical_tool_threshold
        async with owner_sessionmaker() as db:
            later = await db.get(RuntimeTask, run_id)
            assert later.status == "failed" and later.metadata_json["error_code"] == "identical_terminal_task_read"

    seed, _ = await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        before_notification=read_until_guard)
    assert seed["return_id"] is None, "A failed turn still consumed its settled child reads"
