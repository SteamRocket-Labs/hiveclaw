"""S0d: pasted Web secrets stay masked through real colleague delegation."""

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, text

from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.capability_policy import CapabilityPolicy
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation, SessionTurnInput
from app.models.user import User
from tests.integration import test_web_chat_worker_restart_kernel_recovery as kernel_cases
from tests.integration.test_failed_delegation_continuation import _call, _execute
from tests.integration.test_r19a_consumed_result import auxiliary_models  # noqa: F401
from tests.integration.test_r19b_delegation_result_postgres import _tool_results
from tests.integration.test_r20b_empty_search_summary import _bind_database
from tests.services.test_pasted_secret_policy_r2 import assert_no_fragments
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url", "auxiliary_models")
SECRET = "Q7v!s9Lm2xR4"
PLACEHOLDER = "[REDACTED_SECRET]"
PASTE = f"Review the deployment note. password: {SECRET}"
MASKED = PASTE.replace(SECRET, PLACEHOLDER)
COLLEAGUE = "S0d colleague"


def _values(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


def _protected(value, *, placeholder=True):
    serialized = json.dumps(value, ensure_ascii=False, default=str)
    assert_no_fragments(serialized, SECRET)
    if placeholder:
        assert MASKED in serialized, serialized


@pytest.mark.parametrize("repetition", range(1, 4))
async def test_pasted_secret_stays_masked_in_colleague_transport_and_storage(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, repetition,
):
    import app.database as database
    import app.runtime.invoker as invoker
    import app.services.tenant_resolver as tenant_resolver
    from app.agents import orchestrator
    from app.config import get_settings
    from app.database import pin_rls_tenant_context
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.secrets_provider import get_secrets_provider
    from app.services.session_live_input import submit_live_human_input
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    tenant_id, user_id, parent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    child_agent_id = uuid4()
    seed = {"tenant_id": tenant_id, "agent_id": parent_id, "data_root": tmp_path / "agents"}
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(agent_manager, "settings", settings)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    async with owner_sessionmaker() as db:
        models = [LLMModel(tenant_id=tenant_id, provider="openai", model=f"s0d-{lane}", label=lane,
            api_key_encrypted=get_secrets_provider().encrypt("synthetic-provider-only"),
            base_url="http://127.0.0.1:1/v1", max_input_tokens=256_000) for lane in ("parent", "colleague")]
        db.add_all(models)
        await db.flush()
        parent = await db.get(Agent, parent_id)
        parent.owner_user_id, parent.primary_model_id, parent.max_tool_rounds = user_id, models[0].id, 8
        (await db.get(User, user_id)).role = "member"
        (await db.get(ChatSession, session_id)).source_channel = "web"
        child = Agent(id=child_agent_id, tenant_id=tenant_id, name=COLLEAGUE, creator_id=user_id,
            owner_user_id=user_id, primary_model_id=models[1].id, max_tool_rounds=8)
        db.add(child)
        await db.flush()
        for agent in (parent, child):
            await AgentManager().initialize_agent_files(db, agent, original_author_id=user_id)
        db.add(CapabilityPolicy(tenant_id=tenant_id, capability="agent.message.send",
            allowed=True, requires_approval=False))
        await db.commit()

    # Reuse the delegation fixtures' isolated persistence/context seams, while
    # retaining real model selection, provider serialization, tools and policy.
    native_config, native_tools = invoker._resolve_runtime_config, invoker.get_agent_tools_for_llm
    native_client, native_tenant = invoker.create_llm_client, tenant_resolver.resolve_tenant_for_agent
    _bind_database(monkeypatch, app_user_sessionmaker)
    kernel_cases._patch_runtime_seams(monkeypatch, seed, app_user_sessionmaker)
    kernel_cases._patch_invoker_seams(monkeypatch, None, seed, app_user_sessionmaker)
    monkeypatch.setattr(invoker, "_resolve_runtime_config", native_config)
    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", native_tools)
    monkeypatch.setattr(invoker, "create_llm_client", native_client)
    monkeypatch.setattr(invoker, "tenant_scoped_session", database.tenant_scoped_session)
    monkeypatch.setattr(invoker, "execute_tool", agent_tools.execute_tool)
    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", native_tenant)

    async def no_wakeup(**_kwargs):
        return None

    async def no_memory_model(_tenant_id):
        return None

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    monkeypatch.setattr("app.services.session_memory._get_session_memory_model_config", no_memory_model)
    requests = {"s0d-parent": [], "s0d-colleague": []}
    delegated = []

    async def provider_transport(_transport, request):
        assert str(request.url) == "http://127.0.0.1:1/v1/chat/completions"
        body = json.loads(await request.aread())
        lane = body["model"]
        requests[lane].append(body)
        _protected(body)
        if lane == "s0d-parent" and len(requests[lane]) == 2:
            # Copy only what actually reached the parent provider, rather than
            # constructing an already-masked delegation instruction in the test.
            material = next(message["content"] for message in body["messages"]
                if message["role"] == "user" and "Review the deployment note." in str(message["content"]))
            arguments = {"agent_name": COLLEAGUE, "message": material,
                "expected_output": "Review the deployment note without requesting credentials.",
                "tool_profile": "worker_safe"}
            delegated.append(arguments)
            message = {"role": "assistant", "content": "",
                "tool_calls": [_call("delegate_to_agent", arguments, "s0d-delegate")]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": (
                "The masked deployment note has been reviewed." if lane == "s0d-colleague"
                else "The deployment note is recorded." if len(requests[lane]) == 1
                else "The colleague is reviewing the deployment note.")}
            finish = "stop"
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if body.get("stream"):
            delta = dict(message)
            if "tool_calls" in delta:
                delta["tool_calls"] = [{"index": index, **call} for index, call in enumerate(delta["tool_calls"])]
            payload = {"id": "s0d-response", "model": lane,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], "usage": usage}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                content=f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n", request=request)
        return httpx.Response(200, json={"id": "s0d-response", "model": lane,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage},
            request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", provider_transport)

    async def member_input(content):
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            assert (await db.execute(text(
                "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"))).scalar() is False
            receipt = await submit_live_human_input(db=db, agent=await db.get(Agent, parent_id),
                user=await db.get(User, user_id), session=await db.get(ChatSession, session_id),
                content=content, display_content=content, source="web", source_channel="web")
            assert receipt["dispatch_status"] == "dispatched", receipt
            row = await db.get(SessionTurnInput, UUID(receipt["input_id"]))
            if content == PASTE:
                assert receipt["pasted_credential_notice"] is True
                assert row.content_parts_json[0]["text"] == MASKED
            await db.commit()
            return UUID(receipt["run"]["run_id"])

    first_id = await member_input(PASTE)
    await _execute(owner_sessionmaker, first_id, "s0d-paste")
    async def summarize(messages, **_kwargs):
        _protected(messages)
        return "Deployment note recorded."

    processor = WebTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=seed["data_root"],
        summary_generator=summarize)
    drained = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
        tenant_id=tenant_id, worker_id="s0d-paste-terminal", canonical_validator=processor.validate,
        process_callback=processor, task_types=("web_chat_turn",))
    assert drained["delivered"] == 1, drained
    parent_run_id = await member_input(f"/agent {COLLEAGUE} Review the deployment note I just sent.")
    await _execute(owner_sessionmaker, parent_run_id, "s0d-parent")
    results = await _tool_results(owner_sessionmaker, parent_run_id)
    assert "s0d-delegate" in results, (results, {lane: len(items) for lane, items in requests.items()})
    tool_receipt = json.loads(results["s0d-delegate"]["content"])
    assert tool_receipt["ok"] is True, tool_receipt
    assert tool_receipt["target_agent_id"] == str(child_agent_id), tool_receipt
    async with owner_sessionmaker() as db:
        parent_run = await db.get(RuntimeTask, parent_run_id)
        assert parent_run.status == "completed", (parent_run.result_summary, parent_run.metadata_json)
        child_task = (await db.scalars(select(RuntimeTask).where(
            RuntimeTask.root_runtime_task_id == parent_run_id, RuntimeTask.task_type == "delegation"))).one()
        assert child_task.child_agent_id == child_agent_id and child_task.root_user_id == user_id
        _protected(_values(child_task))
        assert child_task.metadata_json["execution_receipt"]
        _protected(child_task.metadata_json["execution_receipt"], placeholder=False)
        child_task.status, child_task.claimed_by, child_task.claim_version = "running", "s0d-colleague", 1
        child_task.attempt_count = 1
        child_task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        child_task_id, child_session_id = child_task.id, UUID(child_task.child_session_id)
        await db.commit()

    fence = set_runtime_task_fence(task_id=child_task_id, worker_id="s0d-colleague", claim_version=1)
    try:
        assert await orchestrator.dispatch_persisted_async_delegation(child_task_id.hex, wait_for_completion=True)
    finally:
        reset_runtime_task_fence(fence)

    assert len(requests["s0d-parent"]) == 3 and len(requests["s0d-colleague"]) == 1, requests
    assert delegated[0]["message"] == MASKED
    _protected(results, placeholder=False)
    async with owner_sessionmaker() as db:
        child_task = await db.get(RuntimeTask, child_task_id)
        assert child_task.status == "completed", (child_task.result_summary, child_task.metadata_json)
        _protected(_values(child_task))
        transcript = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == child_session_id)))
        messages = list(await db.scalars(select(ChatMessage).where(
            ChatMessage.agent_id == child_agent_id)))
        assert transcript and messages
        assert {UUID(message.conversation_id) for message in messages} == {child_session_id}
        _protected([_values(event) for event in transcript])
        _protected([_values(message) for message in messages])
        models = list(await db.scalars(select(SessionModelResult).where(
            SessionModelResult.run_id.in_([first_id, parent_run_id, child_task_id]))))
        assert len(models) == 4 and all(model.state == "round_committed" for model in models)
        _protected([_values(model) for model in models])
        invocation = (await db.scalars(select(SessionToolInvocation).where(
            SessionToolInvocation.run_id == parent_run_id))).one()
        _protected(_values(invocation))
    print(f"S0D repetition={repetition} delegation=completed parent_requests=3 colleague_requests=1 "
        "parent_wire=masked colleague_wire=masked transcript=masked task=masked receipts=no_secret "
        "model_receipts=4")
