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


async def _read_completed_result(owner, monkeypatch, seed, reader, *, fail_after_read=False):
    """Read through the real kernel, then finish or time out before notification delivery."""
    import json
    import app.runtime.invoker as invoker
    from app.services.llm_client import LLMError
    from app.services.web_chat_runtime import start_web_chat_run

    async with owner() as db:
        notice = await db.scalar(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == str(seed["child_id"])))
        seed["result_ref"] = notice.result_ref
        queued = await start_web_chat_run(db=db, agent=await db.get(Agent, seed["agent_id"]),
            user=await db.get(User, seed["user_id"]), session=await db.get(ChatSession, seed["session_id"]),
            content="Read the colleague result.", extra_metadata={"source": "websocket_message"})
        run_id = UUID(queued["run_id"])
        await db.commit()
    arguments = {"check_async_task": {"task_id": str(seed["child_id"])},
        "task_output": {"runtime_task_id": str(seed["child_id"])}, "list_async_tasks": {},
        "read_runtime_result": {"result_ref": seed["result_ref"]}}[reader]
    client = ScriptedLLMClient([{"tool_calls": [_call(reader, arguments, "r3-read")],
        "finish_reason": "tool_calls"}, LLMError("Provider stream delivery is unknown: ReadTimeout: timed out")
        if fail_after_read else {"content": "I read the colleague's summary."}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    await _execute(owner, run_id, "r19a-r3-later-read")
    assert len(client.requests) == 2
    async with owner() as db:
        later = await db.get(RuntimeTask, run_id)
        assert later.status == ("failed" if fail_after_read else "completed"), later.metadata_json
        if fail_after_read:
            assert later.metadata_json["error_code"] == "timeout"
        events = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.run_id == run_id, ChatTranscriptEvent.item_kind == "tool_result")))
        assert len(events) == 1
        payload = events[0].metadata_json["v2_payload"]
        assert payload["outcome"] == "success"
        content = payload.get("model_visible_content", payload["content"])
    body = json.loads(content)
    if reader == "list_async_tasks":
        body = next(row for row in body if UUID(row["task_id"]) == seed["child_id"])
    elif reader == "task_output":
        body = body["task"]
    if reader == "read_runtime_result":
        assert body["status"] == "ok" and body["complete"] is True
    else:
        assert body["status"] == "completed"
        assert body["result"] == "Three sales points: quality, service, value."


@pytest.mark.parametrize("reader", ["check_async_task", "task_output", "list_async_tasks"])
@pytest.mark.parametrize("has_artifact", [False, True])
async def test_summary_read_does_not_consume_a_corrected_or_artifact_result_page(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, auxiliary_models, reader, has_artifact,
):
    from uuid import uuid4
    from app.models.audit import ChatMessage
    from app.models.chat_artifact import ChatArtifact

    async def add_artifact(seed):
        if not has_artifact:
            return
        async with owner_sessionmaker() as db:
            child = await db.get(RuntimeTask, seed["child_id"])
            message = ChatMessage(id=uuid4(), agent_id=child.child_agent_id, tenant_id=child.tenant_id,
                role="assistant", content="Report attached.", conversation_id=child.child_session_id)
            db.add(message)
            await db.flush()
            # A committed child artifact fixture; collection, manifest and notification are native.
            db.add(ChatArtifact(id=uuid4(), agent_id=child.child_agent_id, tenant_id=child.tenant_id,
                session_id=UUID(child.child_session_id), message_id=message.id, runtime_task_id=child.id,
                owner_user_id=seed["user_id"], root_session_id=seed["session_id"], path="workspace/report.md",
                name="report.md", snapshot_hash="a" * 64, snapshot_json={"revision_id": "a" * 64}))
            await db.commit()

    async def read_before_notice(seed):
        await _read_completed_result(owner_sessionmaker, monkeypatch, seed, reader)

    seed, _ = await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        child_success=True, target_artifact_path=None if has_artifact else "workspace/missing.md",
        before_child_completion=add_artifact, before_notification=read_before_notice)
    item = seed["delivery_page"].manifest["items"][0]
    assert item["terminal_status"] == ("completed" if has_artifact else "blocked")
    assert item["artifact_count"] == int(has_artifact)
    assert seed["return_id"] is not None, "A task summary did not read this page's correction or artifacts"


@pytest.mark.parametrize("reader", ["check_async_task", "read_runtime_result"])
async def test_timeout_after_result_read_still_wakes_the_parent(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, auxiliary_models, reader,
):
    async def read_then_timeout(seed):
        await _read_completed_result(owner_sessionmaker, monkeypatch, seed, reader, fail_after_read=True)

    seed, _ = await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        child_success=True, before_notification=read_then_timeout)
    assert seed["return_id"] is not None, "A model timeout cannot consume the completion wake"


async def test_already_read_normalizes_manifest_task_uuid(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, auxiliary_models,
):
    from app.services.agent_session_continuation import _result_page_already_read

    async def read_before_notice(seed):
        await _read_completed_result(owner_sessionmaker, monkeypatch, seed, "read_runtime_result")

    async def check_hex_key(seed, page):
        items = [{**item, "source_run_id": UUID(item["source_run_id"]).hex}
            for item in page.manifest["items"]]
        async with owner_sessionmaker() as db:
            assert await _result_page_already_read(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                session_id=seed["session_id"], user_id=seed["user_id"], items=items)

    seed, _ = await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        child_success=True, before_notification=read_before_notice, before_page_delivery=check_hex_key)
    assert seed["return_id"] is None


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
    assert seed["return_id"] is None, "The terminal-read guard already consumed this result before stopping"


@pytest.mark.parametrize("interrupted", [False, True])
async def test_consumed_result_settles_a_wake_reserved_before_interruption(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, auxiliary_models, interrupted,
):
    import app.runtime.invoker as invoker
    from app.models.runtime_budget import RuntimeBudgetRun, RuntimeBudgetEvent
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.services.web_chat_runtime import start_web_chat_run

    observed = {}

    async def read_once(seed):
        async with owner_sessionmaker() as db:
            queued = await start_web_chat_run(db=db, agent=await db.get(Agent, seed["agent_id"]),
                user=await db.get(User, seed["user_id"]), session=await db.get(ChatSession, seed["session_id"]),
                content="Read the colleague's result.", extra_metadata={"source": "websocket_message"})
            run_id = UUID(queued["run_id"])
            await db.commit()
        client = ScriptedLLMClient([{"tool_calls": [_call("check_async_task", {"task_id": str(seed["child_id"])},
            "reserved-read")], "finish_reason": "tool_calls"}, {"content": "The colleague's points are available."}])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
        await _execute(owner_sessionmaker, run_id, "r19a-reserved-parent")

    native_settle = RuntimeBudgetService.settle

    async def reserve_before_delivery(seed, page):
        observed.update(seed=seed, page=page, key=f"runtime_result_page:{page.id}:continuation")
        service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
        await service.reserve(RuntimeBudgetReservation(budget_run_id=seed["budget_run_id"],
            reservation_key=observed["key"], continuation_wakes=1))
        async with owner_sessionmaker() as db:
            run = await db.get(RuntimeBudgetRun, seed["budget_run_id"])
            assert run.reserved_continuation_wakes == 1
            observed["used"] = run.used_continuation_wakes
        if interrupted:
            async def crash(service, settlement):
                if settlement.reservation_key == observed["key"]:
                    raise RuntimeError("reserved wake settlement interrupted")
                return await native_settle(service, settlement)
            monkeypatch.setattr(RuntimeBudgetService, "settle", crash)

    async def execute():
        return await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
            child_success=True, before_notification=read_once, before_page_delivery=reserve_before_delivery)

    if interrupted:
        with pytest.raises(RuntimeError, match="reserved wake settlement interrupted"):
            await execute()
        monkeypatch.setattr(RuntimeBudgetService, "settle", native_settle)
        service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
        receipt = await service._deliver_page(observed["page"])
        assert receipt["deduplicated"] is True
        await service._mark_page_delivered(page=observed["page"], worker_id="r13b-return", receipt=receipt)
        seed = observed["seed"]
    else:
        seed, _ = await execute()
        assert seed["return_id"] is None
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeBudgetRun, seed["budget_run_id"])
        assert run.reserved_continuation_wakes == 0
        assert run.used_continuation_wakes == observed["used"]
        settlements = list(await db.scalars(select(RuntimeBudgetEvent).where(
            RuntimeBudgetEvent.budget_run_id == seed["budget_run_id"],
            RuntimeBudgetEvent.reservation_key == observed["key"], RuntimeBudgetEvent.event_type == "settlement")))
        assert len(settlements) == 1 and settlements[0].amounts_json["continuation_wakes"] == 0
        notifications = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == seed["session_id"], ChatTranscriptEvent.event_type == "agent_task_notification")))
        assert len(notifications) == 1
        extra = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.parent_agent_id == seed["agent_id"],
            RuntimeTask.metadata_json["integration_page_id"].astext == str(observed["page"].id))))
        assert not extra


async def test_already_started_continuation_keeps_its_existing_wake_charge(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, auxiliary_models,
):
    import app.runtime.invoker as invoker
    from app.models.runtime_budget import RuntimeBudgetRun, RuntimeBudgetEvent
    from app.services.runtime_budget_service import RuntimeBudgetService
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService

    observed = {}
    native_settle = RuntimeBudgetService.settle

    async def interrupt_after_start(seed, page):
        observed.update(seed=seed, page=page, key=f"runtime_result_page:{page.id}:continuation")
        async def crash(service, settlement):
            if settlement.reservation_key == observed["key"]:
                raise RuntimeError("started wake settlement interrupted")
            return await native_settle(service, settlement)
        monkeypatch.setattr(RuntimeBudgetService, "settle", crash)

    with pytest.raises(RuntimeError, match="started wake settlement interrupted"):
        await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
            child_success=True, before_page_delivery=interrupt_after_start)
    monkeypatch.setattr(RuntimeBudgetService, "settle", native_settle)
    seed, page = observed["seed"], observed["page"]
    async with owner_sessionmaker() as db:
        returned = await db.scalar(select(RuntimeTask).where(RuntimeTask.parent_agent_id == seed["agent_id"],
            RuntimeTask.metadata_json["integration_page_id"].astext == str(page.id)))
        assert returned is not None and returned.status == "pending"
        run_id = returned.id
    client = ScriptedLLMClient([{"tool_calls": [_call("check_async_task", {"task_id": str(seed["child_id"])},
        "started-read")], "finish_reason": "tool_calls"}, {"content": "The result has been integrated."}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    await _execute(owner_sessionmaker, run_id, "r19a-started-wake")
    assert len(client.requests) == 2
    service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
    receipt = await service._deliver_page(page)
    assert receipt["deduplicated"] is True
    await service._mark_page_delivered(page=page, worker_id="r13b-return", receipt=receipt)
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeBudgetRun, seed["budget_run_id"])
        assert run.reserved_continuation_wakes == 0 and run.used_continuation_wakes == 1
        settlements = list(await db.scalars(select(RuntimeBudgetEvent).where(
            RuntimeBudgetEvent.budget_run_id == seed["budget_run_id"],
            RuntimeBudgetEvent.reservation_key == observed["key"], RuntimeBudgetEvent.event_type == "settlement")))
        assert len(settlements) == 1 and settlements[0].amounts_json["continuation_wakes"] == 1
        continuations = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.parent_agent_id == seed["agent_id"],
            RuntimeTask.metadata_json["integration_page_id"].astext == str(page.id))))
        assert [task.id for task in continuations] == [run_id]
