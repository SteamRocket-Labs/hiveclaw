"""Provider request regression for a greeting after earlier work."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from tests.integration import test_unverified_t2_recovery_postgres as support
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient
from tests.services.test_session_semantic_history import _submit_turn

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
t2_work = support.t2_work


def _tool_round(call_id, name, arguments):
    return {
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
        "finish_reason": "tool_calls",
    }


async def test_hello_carries_memory_without_work_guidance_or_prior_todos(
    t2_work, owner_sessionmaker, monkeypatch, drain_terminal_boundary_for_task, tmp_path,
):
    from app.memory.write_gate import MemoryWriteDecision
    from app.models.agent import Agent
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from app.runtime import invoker
    from app.services.llm_client import OpenAICompatibleClient
    from app.services.runtime_control_bus import bridge_transcript_event_to_t0
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.runtime_task_worker import drain_web_terminal_boundary_outbox_once
    from app.services.web_chat_runtime import execute_web_chat_run

    seed = t2_work
    preference = "Weekly reports have three bullets of at most twenty characters each."

    async def review_memory(content, *, category, **_kwargs):
        return MemoryWriteDecision(content, content, category, "PL1_public")

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", review_memory)
    old_title = "Publish the prior weekly report"
    other_title = "Review last week with finance"
    old_finding = "Previous reporting figures reconciled"
    old_failure = "Previous publisher refused the report"
    todo_round = _tool_round("todo-1", "track_todo", {"action": "add", "title": old_title, "status": "in_progress"})
    todo_round["tool_calls"] += _tool_round("clock-with-todo", "get_current_time", {})["tool_calls"]
    first = ScriptedLLMClient([
        _tool_round("preference", "save_memory", {"category": "user", "content": preference}),
        todo_round,
        _tool_round("todo-2", "track_todo", {"action": "add", "title": other_title}),
        _tool_round("finding", "record_finding", {"type": "finding", "summary": old_finding, "trust": "verified"}),
        _tool_round("failure", "record_finding", {"type": "failure", "summary": old_failure}),
        {"content": "The draft is ready."},
    ])
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        model = await db.get(LLMModel, agent.primary_model_id)
        model.provider, model.model = "minimax", "MiniMax-M3.1-flash"
        for capability in ("agent.task.track", "agent.memory.write"):
            db.add(CapabilityPolicy(
                tenant_id=seed["tenant_id"], capability=capability, allowed=True, requires_approval=False,
            ))
        await db.commit()

    async def execute(run_id):
        async with owner_sessionmaker() as db:
            run = await db.get(RuntimeTask, run_id)
            run.status, run.claimed_by, run.claim_version, run.attempt_count = "running", "kernel-worker-1", 1, 1
            run.claim_expires_at = datetime.now(UTC) + timedelta(minutes=10)
            await db.commit()
        fence = set_runtime_task_fence(task_id=run_id, worker_id="kernel-worker-1", claim_version=1)
        try:
            await execute_web_chat_run(run_id)
        finally:
            reset_runtime_task_fence(fence)

    async def settle(run_id):
        async with owner_sessionmaker() as db:
            assert (await db.get(RuntimeTask, run_id)).status == "completed"
            event_ids = list(await db.scalars(select(ChatTranscriptEvent.id).where(
                ChatTranscriptEvent.run_id == run_id,
            )))
        for event_id in event_ids:
            await bridge_transcript_event_to_t0(transcript_event_id=event_id, attempts=1)
        await drain_terminal_boundary_for_task(
            drain_web_terminal_boundary_outbox_once, task_id=run_id, worker_id="ledger-request-boundary",
        )

    with monkeypatch.context() as patch:
        seed_reply = ScriptedLLMClient([{"content": "360"}])
        patch.setattr(invoker, "create_llm_client", lambda **_kwargs: seed_reply)
        await execute(seed["run_id"])
    await settle(seed["run_id"])
    _, prior_run, _ = await _submit_turn(
        owner_sessionmaker, user_id=seed["user_id"], agent_id=seed["agent_id"], session_id=seed["session_id"],
        content=f"Prepare a weekly report draft and remember this preference for next time. {preference}",
    )
    with monkeypatch.context() as patch:
        patch.setattr(invoker, "create_llm_client", lambda **_kwargs: first)
        await execute(prior_run)
    await settle(prior_run)

    _, hello_run, _ = await _submit_turn(
        owner_sessionmaker, user_id=seed["user_id"], agent_id=seed["agent_id"], session_id=seed["session_id"],
        content="hello",
    )
    requests = []

    def capture(request):
        requests.append(json.loads(request.content))
        reply = {"choices": [{"delta": {"content": "Hello!"}, "finish_reason": "stop"}]}
        return httpx.Response(200, text=f"data: {json.dumps(reply)}\n\ndata: [DONE]\n\n")

    async def get_client(client):
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(capture))
        return client._client

    with monkeypatch.context() as patch:
        patch.setattr(OpenAICompatibleClient, "_get_client", get_client)
        await execute(hello_run)
    await settle(hello_run)
    async with owner_sessionmaker() as db:
        assert await db.scalar(select(SessionToolInvocation.id).where(SessionToolInvocation.run_id == hello_run)) is None

    (hello,) = requests
    (tmp_path / "hello-provider-request.json").write_text(json.dumps(hello, ensure_ascii=False), encoding="utf-8")
    assert hello["model"] == "MiniMax-M3.1-flash"
    assert any(m["role"] == "user" and m["content"] == "hello" for m in hello["messages"])
    assert any(m.get("tool_call_id") == "clock-with-todo" for m in hello["messages"])
    assert any(
        isinstance(m.get("content"), str) and m["content"].startswith("- id=explicit_") and preference in m["content"]
        for m in hello["messages"]
    )

    _, read_run, _ = await _submit_turn(
        owner_sessionmaker, user_id=seed["user_id"], agent_id=seed["agent_id"], session_id=seed["session_id"],
        content="Read the previous request's work ledger.",
    )
    reader = ScriptedLLMClient([_tool_round("requested-ledger", "read_ledger", {}), {"content": "I read it."}])
    with monkeypatch.context() as patch:
        patch.setattr(invoker, "create_llm_client", lambda **_kwargs: reader)
        await execute(read_run)
    (read_result,) = [m for m in reader.requests[-1]["messages"] if m.get("tool_call_id") == "requested-ledger"]
    ledger = json.loads(read_result["content"])["ledger"]
    assert {item["title"]: item["status"] for item in ledger["todo_items"]} == {
        old_title: "in_progress", other_title: "pending",
    }
    assert [item["summary"] for item in ledger["findings"]] == [old_finding]
    assert [item["error"] for item in ledger["failures"]] == [old_failure]

    first_request = json.dumps(hello["messages"], ensure_ascii=False)
    forbidden = (
        old_title, other_title, old_finding, old_failure,
        "Recorded Work Ledger snapshot", "Progress Ledger advisory", "Work Ledger — Reboot After Compaction",
        "track_todo", "read_ledger", "list_triggers", "Work board: use",
        "Check your ledger", "Check your triggers", "Check your workspace",
    )
    assert [fragment for fragment in forbidden if fragment.casefold() in first_request.casefold()] == []
    todo_tool = next(tool["function"] for tool in hello["tools"] if tool["function"]["name"] == "track_todo")
    assert "proactively" not in todo_tool["description"]
