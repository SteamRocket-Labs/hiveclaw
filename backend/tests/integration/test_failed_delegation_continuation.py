"""Failed colleagues remain readable, and return turns retain the parent's limits."""

import json
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.capability_policy import CapabilityPolicy
from app.models.llm import LLMModel
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from tests.integration import test_web_chat_worker_restart_kernel_recovery as kernel_cases

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
FAILURE = "同事这次用完了用量预算，没有完成。"


def _call(name, arguments, identity):
    return {"id": identity, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}


async def _execute(owner, run_id, worker):
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run

    async with owner() as db:
        task = await db.get(RuntimeTask, run_id)
        task.status, task.claimed_by, task.claim_version, task.attempt_count = "running", worker, 1, 1
        task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        await db.commit()
    token = set_runtime_task_fence(task_id=run_id, worker_id=worker, claim_version=1)
    try:
        await execute_web_chat_run(run_id)
    finally:
        reset_runtime_task_fence(token)


async def _failed_child(owner, app_factory, monkeypatch, tmp_path, *, parent_rounds=8, parent_tokens=30):
    import app.database as database
    import app.runtime.invoker as invoker
    import app.services.tenant_resolver as tenant_resolver
    from app.agents import orchestrator
    from app.config import get_settings
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.secrets_provider import get_secrets_provider

    seed = await kernel_cases._seed_run(owner, tmp_path, prompt="Ask the colleague for three work points, then summarize.")
    seed["child_agent_id"] = uuid4()
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(agent_manager, "settings", settings)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    async with owner() as db:
        parent = await db.get(Agent, seed["agent_id"])
        parent.max_tool_rounds = parent_rounds
        root = await db.get(RuntimeTask, seed["run_id"])
        root.metadata_json = {**root.metadata_json, "source": "websocket_message"}
        await AgentManager().initialize_agent_files(db, parent, original_author_id=seed["user_id"])
        model = await db.get(LLMModel, parent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-local-provider-only")
        model.max_input_tokens = 256_000
        child = Agent(id=seed["child_agent_id"], tenant_id=seed["tenant_id"], name="Weekly colleague",
            creator_id=seed["user_id"], owner_user_id=seed["user_id"], primary_model_id=model.id)
        db.add(child)
        await db.flush()
        await AgentManager().initialize_agent_files(db, child, original_author_id=seed["user_id"])
        for capability in ("agent.message.send", "agent.async_task.read", "agent.context.read"):
            db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability=capability,
                allowed=True, requires_approval=False))
        await db.commit()

    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_factory)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)

    parent_client = kernel_cases.ScriptedLLMClient([
        {"tool_calls": [_call("send_message_to_agent", {"agent_name": "Weekly colleague",
            "message": "List three work points.", "msg_type": "consult"}, "delegate")],
            "usage": {"total_tokens": parent_tokens}, "finish_reason": "tool_calls"},
        {"content": "同事正在处理。", "usage": {"total_tokens": 1}},
    ])
    native_config, native_tools = invoker._resolve_runtime_config, invoker.get_agent_tools_for_llm
    native_resolve_tenant = tenant_resolver.resolve_tenant_for_agent
    kernel_cases._patch_runtime_seams(monkeypatch, seed, app_factory)
    kernel_cases._patch_invoker_seams(monkeypatch, parent_client, seed, app_factory)
    monkeypatch.setattr(invoker, "_resolve_runtime_config", native_config)
    monkeypatch.setattr(invoker, "tenant_scoped_session", database.tenant_scoped_session)
    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", native_tools)
    monkeypatch.setattr(invoker, "execute_tool", agent_tools.execute_tool)
    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", native_resolve_tenant)

    from app.database import pin_rls_tenant_context
    from app.models.chat_session import ChatSession
    from app.models.user import User
    from app.services.runtime_budget_failover import apply_runtime_budget_root_binding
    from app.services.web_chat_runtime import _create_runtime_budget_root_run_for_chat

    async with app_factory() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        binding = await _create_runtime_budget_root_run_for_chat(db=db,
            agent=await db.get(Agent, seed["agent_id"]), user=await db.get(User, seed["user_id"]),
            session=await db.get(ChatSession, seed["session_id"]), run_uuid=seed["run_id"],
            source="web", profile="web_chat_turn", interactive=True)
        assert binding.budget_run_id is not None
        root = await db.get(RuntimeTask, seed["run_id"])
        root.budget_run_id, root.budget_admission_status = binding.budget_run_id, "root"
        root.metadata_json = apply_runtime_budget_root_binding(root.metadata_json, binding)
        seed["budget_run_id"] = binding.budget_run_id
        await db.commit()

    async def no_wakeup(**_kwargs):
        return None

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    await _execute(owner, seed["run_id"], "r13b-parent")
    async with owner() as db:
        root = await db.get(RuntimeTask, seed["run_id"])
        assert root.status == "completed", root.result_summary
        child = await db.scalar(select(RuntimeTask).where(RuntimeTask.root_runtime_task_id == root.id,
            RuntimeTask.task_type == "delegation"))
        assert child is not None, root.result_summary
        seed["child_id"] = child.id
        child.status, child.claimed_by, child.claim_version, child.attempt_count = "running", "r13b-child", 1, 1
        child.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        await db.commit()
    child_client = kernel_cases.ScriptedLLMClient([{
        "tool_calls": [_call("get_current_time", {}, "child-check")],
        "usage": {"total_tokens": 1_000_001}, "finish_reason": "tool_calls",
    }])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: child_client)
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence

    token = set_runtime_task_fence(task_id=seed["child_id"], worker_id="r13b-child", claim_version=1)
    try:
        assert await orchestrator.dispatch_persisted_async_delegation(seed["child_id"].hex, wait_for_completion=True)
    finally:
        reset_runtime_task_fence(token)
    async with owner() as db:
        child = await db.get(RuntimeTask, seed["child_id"])
        assert child.status == "failed" and child.metadata_json["terminal_reason"] == "tool_budget", child.metadata_json
        assert len(child_client.requests) == 1

    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_factory, data_root=seed["data_root"])
    drained = await RuntimeTerminalBoundaryOutboxService(session_factory=app_factory).drain_once(
        tenant_id=seed["tenant_id"], worker_id="r13b-terminal", canonical_validator=processor.validate,
        process_callback=processor, task_types=("delegation",))
    assert drained["delivered"] == 1, drained

    service = RuntimeNotificationOutboxService(session_factory=app_factory)
    claimed = await service.claim_batch(worker_id="r13b-return", limit=100)
    pages = await service.prepare_integration_pages(worker_id="r13b-return",
        claimed=[item for item in claimed if item.tenant_id == seed["tenant_id"]])
    assert len(pages) == 1
    delivered = await service._deliver_page(pages[0])
    assert delivered.get("ok") is True, delivered
    async with owner() as db:
        notification = await db.scalar(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == str(seed["child_id"])))
        returned = await db.scalar(select(RuntimeTask).where(RuntimeTask.parent_agent_id == seed["agent_id"],
            RuntimeTask.metadata_json["integration_page_id"].astext == str(pages[0].id)))
        assert returned is not None and returned.root_runtime_task_id == seed["run_id"]
        seed["budget_inherited"] = returned.budget_run_id == seed["budget_run_id"]
        print("R13B_BUDGET_INHERITED", seed["budget_inherited"], "admission", returned.budget_admission_status)
        seed.update(return_id=returned.id, result_ref=notification.result_ref)
    return seed, parent_client


async def test_result_return_keeps_the_original_runtime_budget(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    seed, _ = await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    assert seed["budget_inherited"], "A completion return must not allocate a fresh root budget"


async def test_failed_child_reason_reaches_parent_through_all_existing_readers(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import app.runtime.invoker as invoker
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionToolInvocation
    from app.services.session_semantic_history import _event_payload

    seed, _ = await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    reads = [
        _call("read_runtime_result", {"result_ref": seed["result_ref"]}, "read-result"),
        _call("task_output", {"runtime_task_id": str(seed["child_id"])}, "read-output"),
        _call("check_async_task", {"task_id": str(seed["child_id"])}, "read-status"),
    ]
    client = kernel_cases.ScriptedLLMClient([
        {"tool_calls": reads, "finish_reason": "tool_calls"}, {"content": FAILURE},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    await _execute(owner_sessionmaker, seed["return_id"], "r13b-reader")
    assert len(client.requests) == 2
    observed = {message["tool_call_id"]: message["content"] for message in client.requests[1]["messages"]
        if message.get("role") == "tool" and message.get("tool_call_id") in {call["id"] for call in reads}}
    async with owner_sessionmaker() as db:
        raw = {}
        for call in await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == seed["return_id"])):
            if call.result_event_id:
                event = await db.get(ChatTranscriptEvent, call.result_event_id)
                body = json.loads(_event_payload(event)["content"])
                task = body.get("task") or body
                raw[call.tool_name] = {key: task.get(key) for key in ("status", "result", "reason_code", "reason")}
        print("R13B_RAW_READERS", json.dumps(raw, ensure_ascii=False))
    print("R13B_FAILED_CHILD_READERS", json.dumps(observed, ensure_ascii=False))
    (tmp_path / "reader-observations.json").write_text(json.dumps(observed, ensure_ascii=False, indent=2))
    for identity in ("read-result", "read-output", "read-status"):
        assert identity in observed and "用量预算" in observed[identity], observed
        assert "failed" in observed[identity], observed
        assert "1000001" not in observed[identity] and "[Runtime Limit]" not in observed[identity]
    async with owner_sessionmaker() as db:
        events = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.run_id == seed["return_id"], ChatTranscriptEvent.item_kind == "assistant_final")))
        assert len(events) == 1
        child = await db.get(RuntimeTask, seed["child_id"])
        assert child.result_summary == "这次用完了用量预算，没有完成。"
    # Native finals reference the text blocks delivered by the member API.
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api import chat_sessions
    from app.core.security import get_current_user
    from app.database import get_db, pin_rls_tenant_context
    from app.models.user import User

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db

    async def member():
        async for db in scoped_db():
            return await db.get(User, seed["user_id"])

    app = FastAPI()
    app.include_router(chat_sessions.router)
    app.dependency_overrides[get_current_user] = member
    app.dependency_overrides[get_db] = scoped_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as client:
        response = await client.get(f"/agents/{seed['agent_id']}/sessions/{seed['session_id']}/transcript",
            params={"schema_version": 2, "limit": 1000})
    assert response.status_code == 200, response.text
    finals = [event for event in response.json() if event.get("item_kind") == "assistant_final"
        and event.get("scope", {}).get("run_id") == str(seed["return_id"])]
    assert len(finals) == 1
    from app.services.session_event_contract import SESSION_EVENT_SCHEMA, reduce_session_events

    items = reduce_session_events(event for event in response.json() if event.get("schema") == SESSION_EVENT_SCHEMA).items
    displayed = "".join(items[source["item_id"]].content for source in finals[0]["payload"]["source_blocks"])
    assert displayed == FAILURE
    assert sum(event["payload"].get("content") == FAILURE for event in response.json()) == 1


@pytest.mark.parametrize("bound", ["rounds", "tokens"])
async def test_polling_return_turn_does_not_reset_the_member_turn_limit(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, bound,
):
    import app.runtime.invoker as invoker

    seed, _ = await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        parent_rounds=4, parent_tokens=32_000 if bound == "tokens" else 30)
    client = kernel_cases.ScriptedLLMClient([
        {"tool_calls": [_call("check_async_task", {"task_id": str(seed["child_id"])}, f"poll-{index}")],
            "usage": {"total_tokens": 1000 if bound == "tokens" else 1}, "finish_reason": "tool_calls"}
        for index in range(6)
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    await _execute(owner_sessionmaker, seed["return_id"], "r13b-poller")
    async with owner_sessionmaker() as db:
        returned = await db.get(RuntimeTask, seed["return_id"])
        models = list(await db.scalars(select(SessionModelResult).where(
            SessionModelResult.run_id.in_([seed["run_id"], seed["return_id"]]))))
        print("R13B_POLL_BOUND", bound, "calls", len(client.requests), "committed_rounds", len(models),
            "status", returned.status, "metadata", returned.metadata_json.get("turn_tokens_used"))
        assert returned.status == "failed" and returned.metadata_json["terminal_reason"] == "tool_budget"
        assert len(client.requests) <= (1 if bound == "tokens" else 2), "Continuation reset the member's existing ceiling"
