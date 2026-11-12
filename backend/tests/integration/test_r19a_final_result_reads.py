"""Final review: native skipped pages, running envelopes and failed reader runs."""

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.services.runtime_display_status import DELEGATION_RUNNING_RESULT_READ_NOTE, FAILED_DELEGATION_MODEL_INSTRUCTION
from tests.integration.test_failed_delegation_continuation import _call, _execute, _failed_child
from tests.integration.test_r19a_consumed_result import auxiliary_models, _read_completed_result
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient


async def test_coordination_skipped_page_already_read_does_not_wake_again(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, auxiliary_models,
):
    import app.runtime.invoker as invoker
    from app.agents import orchestrator
    from app.services.web_chat_runtime import start_web_chat_run

    contender = f"runtime-task:{uuid4().hex}"
    held = {}

    async def hold_equivalent_work(seed):
        async with orchestrator.gateway_scope(tenant_id=seed["tenant_id"]) as gateway:
            gateway_type = type(gateway)
            acquire = gateway_type.acquire_lease

        async def contend(gateway, *, task_key, agent_id, ttl_seconds):
            if task_key.startswith(f"delegation:{seed['agent_id']}:{seed['child_agent_id']}:") and "key" not in held:
                held.update(tenant=seed["tenant_id"], key=task_key)
                lease = await acquire(gateway, task_key=task_key, agent_id=contender, ttl_seconds=600)
                assert lease.acquired
                held["lease_id"] = lease.lease.id
            return await acquire(gateway, task_key=task_key, agent_id=agent_id, ttl_seconds=ttl_seconds)

        monkeypatch.setattr(gateway_type, "acquire_lease", contend)

    async def read_skipped(seed):
        async with owner_sessionmaker() as db:
            queued = await start_web_chat_run(db=db, agent=await db.get(Agent, seed["agent_id"]),
                user=await db.get(User, seed["user_id"]), session=await db.get(ChatSession, seed["session_id"]),
                content="Read the colleague's skipped task.", extra_metadata={"source": "websocket_message"})
            run_id = UUID(queued["run_id"])
            await db.commit()
        client = ScriptedLLMClient([{"tool_calls": [_call("check_async_task",
            {"task_id": str(seed["child_id"])}, "read-skipped")], "finish_reason": "tool_calls"},
            {"content": "The colleague task was skipped."}])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
        await _execute(owner_sessionmaker, run_id, "r19a-r4-skipped-reader")
        assert len(client.requests) == 2
        async with owner_sessionmaker() as db:
            assert (await db.get(RuntimeTask, run_id)).status == "completed"
            event = await db.scalar(select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.run_id == run_id, ChatTranscriptEvent.item_kind == "tool_result"))
            payload = event.metadata_json["v2_payload"]
            assert payload["outcome"] == "success"
            assert json.loads(payload.get("model_visible_content", payload["content"]))["result"] == "这次任务已跳过。"

    try:
        seed, _ = await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
            child_skipped=True, before_parent=hold_equivalent_work, before_notification=read_skipped)
        assert seed["delivery_page"].manifest["items"][0]["terminal_status"] == "blocked"
        assert seed["return_id"] is None, "The parent already read the projected skipped page"
    finally:
        if "lease_id" in held:
            async with orchestrator.gateway_scope(tenant_id=held["tenant"]) as gateway:
                assert await gateway.release_lease(task_key=held["key"], agent_id=contender, lease_id=held["lease_id"])


async def test_running_grandchild_failure_envelope_waits_without_offering_retry(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, auxiliary_models,
):
    seed, _ = await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        child_terminal_reader="check_async_task", nested_running=True)
    async with owner_sessionmaker() as db:
        child = await db.get(RuntimeTask, seed["child_id"])
        grandchild = await db.get(RuntimeTask, seed["nested_id"])
        parent = await db.get(RuntimeTask, seed["return_id"])
        assert UUID(grandchild.parent_session_id) == UUID(child.child_session_id) and grandchild.status == "running"
        assert child.metadata_json["error_code"] == "identical_running_task_read"
        assert DELEGATION_RUNNING_RESULT_READ_NOTE in parent.prompt
        assert FAILED_DELEGATION_MODEL_INSTRUCTION not in parent.prompt


@pytest.mark.parametrize("status,nested_code,expected_consumed", [
    ("needs_reconciliation", "timeout", False),
    ("failed", "identical_terminal_task_read", True),
    ("needs_reconciliation", "identical_terminal_task_read", True),
])
async def test_reader_run_state_and_business_outcome_code(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, auxiliary_models,
    status, nested_code, expected_consumed,
):
    async def read_then_settle(seed):
        await _read_completed_result(owner_sessionmaker, monkeypatch, seed, "read_runtime_result")
        async with owner_sessionmaker() as db:
            event = await db.scalar(select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.session_id == seed["session_id"], ChatTranscriptEvent.item_kind == "tool_result",
                ChatTranscriptEvent.created_at >= (await db.get(RuntimeTask, seed["child_id"])).completed_at))
            reader = await db.get(RuntimeTask, event.run_id)
            # Fixture the two persisted terminal shapes; native reads and notification delivery stay real.
            reader.status = status
            reader.metadata_json = {**reader.metadata_json, "error_code": None, "outcome": {"error_code": nested_code}}
            await db.commit()

    seed, _ = await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        child_success=True, before_notification=read_then_settle)
    assert (seed["return_id"] is None) is expected_consumed
