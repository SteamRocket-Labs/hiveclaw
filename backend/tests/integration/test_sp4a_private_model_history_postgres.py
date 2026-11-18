"""Previously admitted history at the private, group, and approval boundaries."""

import json
from contextvars import ContextVar
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.organization_identity import ContentInputReceipt
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m4d_group_memory_postgres import group_call, group_memory, save_group  # noqa: F401
from tests.integration.test_memory_source_read_work_postgres import memory_dag  # noqa: F401
from tests.integration.test_model_round_read_work_postgres import web_memory_work  # noqa: F401
from tests.integration.test_session_permission_execution_once import _mounted_client
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
PRIVATE_MEMORY = "Recall fixture release MEMORY_DAG_9: retained owner knowledge."
GROUP_MEMORY = "GROUP_RELEASE_MARKER"


def tool_call(call_id, name, arguments):
    return {"tool_calls": [{"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}], "finish_reason": "tool_calls"}


async def revoke_memory(owner_sessionmaker, source_id):
    async with owner_sessionmaker() as db:
        receipt = await db.get(ContentInputReceipt, source_id)
        assert receipt.authority_json["state"] == "active"
        receipt.authority_json = {**receipt.authority_json, "state": "revoked"}
        await db.commit()


def tool_output(request, call_id):
    return next((message["content"] for message in request["messages"]
        if message["role"] == "tool" and message.get("tool_call_id") == call_id), "")


async def run_private_work(n, provider, monkeypatch):
    from app.runtime import invoker
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run

    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
    fence = set_runtime_task_fence(task_id=n.web_run, worker_id="a2b-worker", claim_version=1)
    try:
        await execute_web_chat_run(n.web_run)
    finally:
        reset_runtime_task_fence(fence)


async def test_private_round_retains_revoked_tool_history_without_source_reads(
    web_memory_work, owner_sessionmaker, monkeypatch,
):
    from app.services import session_model_round, source_read_queries

    n = web_memory_work
    active_round = ContextVar("sp4a_preparing_round", default=None)
    validations, catalogue_reads = [], []
    prepare = session_model_round.prepare_model_request
    validate = session_model_round._validate_or_withhold_model_sources
    load_catalogue = source_read_queries.load_source_catalogue

    async def observed_prepare(db, **kwargs):
        token = active_round.set(kwargs["round_index"])
        try:
            return await prepare(db, **kwargs)
        finally:
            active_round.reset(token)

    async def observed_validation(*args, **kwargs):
        validations.append(active_round.get())
        return await validate(*args, **kwargs)

    async def observed_catalogue(*args, **kwargs):
        if active_round.get() is not None:
            catalogue_reads.append(active_round.get())
        return await load_catalogue(*args, **kwargs)

    monkeypatch.setattr(session_model_round, "prepare_model_request", observed_prepare)
    monkeypatch.setattr(session_model_round, "_validate_or_withhold_model_sources", observed_validation)
    monkeypatch.setattr(source_read_queries, "load_source_catalogue", observed_catalogue)

    class Provider(ScriptedLLMClient):
        async def stream(self, **kwargs):
            result = await super().stream(**kwargs)
            if len(self.requests) == 2:
                assert PRIVATE_MEMORY in tool_output(self.requests[-1], "load-memory")
                await revoke_memory(owner_sessionmaker, n.receipts[9].id)
            return result

    provider = Provider([
        tool_call("load-memory", "load_memory", {"ids": ["dag-9"]}),
        tool_call("clock", "get_current_time", {"timezone": "UTC"}),
        {"content": "Done."},
    ])
    await run_private_work(n, provider, monkeypatch)
    assert len(provider.requests) == 3
    earlier = tool_output(provider.requests[1], "load-memory")
    later = tool_output(provider.requests[2], "load-memory")
    print("SP4A_PRIVATE", {"retained": earlier == later, "validation_rounds": validations,
        "catalogue_rounds": sorted(set(catalogue_reads))}, flush=True)
    assert PRIVATE_MEMORY in earlier and later == earlier
    assert validations == [] and catalogue_reads == []


async def test_group_round_withholds_revoked_tool_history(group_memory, owner_sessionmaker, app_user_sessionmaker):
    from app.services.llm_client import LLMMessage
    from app.services.session_model_round import prepare_model_request
    from app.services.session_semantic_history import load_session_semantic_history

    g = group_memory
    await save_group(g, app_user_sessionmaker)
    arguments = {"ids": [g.entry_id]}
    content, earlier_tool = await group_call(g, app_user_sessionmaker, "load_memory", arguments, include_history=True)
    assert GROUP_MEMORY in content
    _, admitted = await group_call(g, app_user_sessionmaker, "load_memory", arguments, include_history=True)
    async with tenant_scoped_session(g.n.tenant, session_factory=app_user_sessionmaker) as db:
        earlier = await db.scalar(select(SessionModelResult).where(
            SessionModelResult.provider_request_id == admitted.provider_request_id))
        earlier_request = earlier.model_request_snapshot_json["wire_request"]
        # Keep the worker's admitted history in memory across the authority change.
        history = await load_session_semantic_history(db, tenant_id=g.n.tenant, agent_id=g.n.agent,
            session_id=g.session, reader_run_id=earlier.run_id)
        messages = [LLMMessage(role=item.role, content=item.content, tool_calls=item.tool_calls,
            tool_call_id=item.tool_call_id, source_ref=item.id) for item in history.messages]
        assert any(GROUP_MEMORY in (item.content or "") and item.tool_call_id == earlier_tool.provider_tool_use_id
            for item in messages)
        await db.commit()
        await revoke_memory(owner_sessionmaker, g.source_id)
        request_id = await prepare_model_request(db, tenant_id=g.n.tenant, agent_id=g.n.agent, session_id=g.session,
            run_id=earlier.run_id, turn_id=earlier.turn_id, round_index=g.rounds[earlier.run_id] + 1,
            messages=messages, tools=[], provider="synthetic", model="group-memory-reference")
        later = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id))
        later_snapshot = later.model_request_snapshot_json
    assert GROUP_MEMORY in tool_output(earlier_request, earlier_tool.provider_tool_use_id)
    assert tool_output(later_snapshot["wire_request"], earlier_tool.provider_tool_use_id) == ""
    withheld = next(entry for entry in later_snapshot["input_bindings"]["messages"]
        if entry["source_ref"] == f"session-tool-result:{earlier_tool.id}")
    assert withheld["status"] == "withheld" and withheld["dependencies"] == []
    print("SP4A_GROUP", withheld["code"], flush=True)


async def test_approved_email_refuses_revoked_private_tool_history(
    web_memory_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.services import agent_tools, delegation_permission_runtime, runtime_task_worker
    from app.services.content_input_receipts import ContentInputReceiptError

    n = web_memory_work
    deliveries, refusals = [], []
    validate = delegation_permission_runtime.validate_permission_input_sources

    async def deliver(_name, _agent_id, _workspace, arguments, **_kwargs):
        deliveries.append(arguments)
        return "Unexpected delivery"

    async def no_wakeup(**_kwargs):
        pass

    async def observed_validation(*args, **kwargs):
        try:
            await validate(*args, **kwargs)
        except ContentInputReceiptError as exc:
            refusals.append(exc.code)
            raise

    monkeypatch.setattr(agent_tools, "_handle_email_tool", deliver)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", no_wakeup)
    monkeypatch.setattr(delegation_permission_runtime, "validate_permission_input_sources", observed_validation)
    provider = ScriptedLLMClient([
        tool_call("load-memory", "load_memory", {"ids": ["dag-9"]}),
        tool_call("send-memory", "send_email", {"to": "recipient@example.test", "subject": "Report", "body": PRIVATE_MEMORY}),
    ])
    await run_private_work(n, provider, monkeypatch)
    assert len(provider.requests) == 2
    assert PRIVATE_MEMORY in tool_output(provider.requests[1], "load-memory")
    async with owner_sessionmaker() as db:
        outward = await db.scalar(select(SessionToolInvocation).where(
            SessionToolInvocation.run_id == n.web_run, SessionToolInvocation.provider_tool_use_id == "send-memory"))
        assert outward.permission_state == "waiting" and outward.result_event_id is None
        assert outward.provider_arguments_json["body"] == PRIVATE_MEMORY and deliveries == []
    await revoke_memory(owner_sessionmaker, n.receipts[9].id)
    async with _mounted_client(app_user_sessionmaker, SimpleNamespace(user_id=n.user, tenant_id=n.tenant)) as client:
        response = await client.post(f"/api/agents/{n.agent}/sessions/{n.web_session}/permissions/{outward.permission_item_id}/resolve",
            json={"action": "allow_once"})
        assert response.status_code == 200, response.text
    async with owner_sessionmaker() as db:
        invocation = await db.get(SessionToolInvocation, outward.id)
        result = await db.get(ChatTranscriptEvent, invocation.result_event_id)
        assert deliveries == [] and invocation.effect_state == "prepared_not_started"
        assert result.metadata_json["v2_payload"]["outcome"] == "failed"
    assert refusals == ["explicit_memory_history_denied"]
    print("SP4A_APPROVAL", {"refusals": refusals, "deliveries": len(deliveries)}, flush=True)
