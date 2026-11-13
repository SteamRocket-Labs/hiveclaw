"""Owned delegation reads keep their input receipts without returning files."""

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.capability_policy import CapabilityPolicy
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.knowledge import KnowledgeDocument, KnowledgeGrant
from app.models.llm import LLMModel
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_result import RuntimeResultObject
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.models.user import User
from tests.integration import test_web_chat_worker_restart_kernel_recovery as kernel_cases
from tests.integration.test_failed_delegation_continuation import _call, _execute
from tests.integration.test_r19a_consumed_result import auxiliary_models  # noqa: F401
from tests.integration.test_r19b_delegation_result_postgres import _tool_results

pytestmark = pytest.mark.usefixtures("migrated_pg_url", "auxiliary_models")
CSV = "product,sales\nAlpha,120\nBeta,90\nGamma,150\n"
RESULT = "R22A_COLLEAGUE_RESULT: total sales 360; Gamma leads with 150."


def _round(name, arguments, identity):
    return {"tool_calls": [_call(name, arguments, identity)], "finish_reason": "tool_calls"}


async def _file_result(owner, app_factory, monkeypatch, tmp_path, *, source_grant=False, before_handback=None):
    import app.database as database
    import app.runtime.invoker as invoker
    import app.services.tenant_resolver as tenant_resolver
    from app.agents import orchestrator
    from app.api import upload
    from app.config import get_settings
    from app.core.security import get_current_user
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.content_input_receipts import latest_workspace_receipt
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_budget_failover import apply_runtime_budget_root_binding
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.secrets_provider import get_secrets_provider
    from app.services.web_chat_runtime import _create_runtime_budget_root_run_for_chat

    seed = await kernel_cases._seed_run(owner, tmp_path, prompt="Delegate the uploaded sales analysis to my colleague.")
    seed["child_agent_id"], seed["other_user_id"] = uuid4(), uuid4()
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(agent_manager, "settings", settings)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    async with owner() as db:
        parent = await db.get(Agent, seed["agent_id"])
        parent.max_tool_rounds = 12
        (await db.get(User, seed["user_id"])).role = "member"
        root = await db.get(RuntimeTask, seed["run_id"])
        root.metadata_json = {**root.metadata_json, "source": "websocket_message"}
        await AgentManager().initialize_agent_files(db, parent, original_author_id=seed["user_id"])
        model = await db.get(LLMModel, parent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-local-provider-only")
        model.max_input_tokens = 256_000
        child = Agent(id=seed["child_agent_id"], tenant_id=seed["tenant_id"], name="R22a colleague",
            creator_id=seed["user_id"], owner_user_id=seed["user_id"], primary_model_id=model.id)
        db.add(child)
        db.add(User(id=seed["other_user_id"], tenant_id=seed["tenant_id"],
            username=f"r22a-{seed['other_user_id'].hex}", email=f"{seed['other_user_id'].hex}@fixture.test",
            password_hash="fixture", display_name="Other member", role="member"))
        await db.flush()
        await AgentManager().initialize_agent_files(db, child, original_author_id=seed["user_id"])
        for capability in ("agent.message.send", "agent.async_task.read", "agent.context.read", "workspace.file.read"):
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
    monkeypatch.setattr(upload, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)

    async def current_user():
        async with app_factory() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            return await db.get(User, seed["user_id"])

    async def scoped_db():
        async with app_factory() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db
            await db.commit()

    app = FastAPI()
    app.include_router(upload.router)
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = scoped_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as client:
        uploaded = await client.post("/chat/upload", data={"agent_id": str(seed["child_agent_id"]),
            "skip_personal_kb": "true"}, files={"file": ("sales.csv", CSV.encode(), "text/csv")})
    assert uploaded.status_code == 200, uploaded.text
    seed["file_path"] = uploaded.json()["workspace_path"]
    async with owner() as db:
        receipt = await latest_workspace_receipt(db, seed["tenant_id"], seed["child_agent_id"], seed["file_path"])
        assert receipt is not None
        seed["file_receipt_id"] = receipt.id
        if source_grant:
            digest = hashlib.sha256(CSV.encode()).hexdigest()
            (seed["data_root"] / "personal-source.md").write_text(CSV)
            document = KnowledgeDocument(id=uuid4(), tenant_id=seed["tenant_id"], scope_type="person",
                scope_id=seed["other_user_id"], owner_user_id=seed["other_user_id"], source_kind="text",
                source_sha256=digest, canonical_md_path="personal-source.md", canonical_md_sha256=digest,
                title="Granted sales source", status="ready")
            db.add(document)
            await db.flush()
            grant = KnowledgeGrant(id=uuid4(), tenant_id=seed["tenant_id"], scope_id=seed["other_user_id"],
                resource_type="document", resource_id=document.id, document_id=document.id,
                grantee_type="user", grantee_id=seed["user_id"], created_by_user_id=seed["other_user_id"], permission="read")
            db.add(grant)
            receipt.dependencies_json = [*receipt.dependencies_json,
                {"source_kind": "knowledge_document", "source_id": str(document.id), "source_version": digest}]
            seed["grant_id"], seed["document_id"] = grant.id, document.id
        await db.commit()

    parent_client = kernel_cases.ScriptedLLMClient([
        _round("delegate_to_agent", {"agent_name": "R22a colleague", "message": f"Read {seed['file_path']} and report the total.",
            "expected_output": "A plain sales summary", "tool_profile": "worker_safe"}, "delegate"),
        {"content": "The colleague is processing the sales file."},
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
        await db.commit()

    async def no_wakeup(**_kwargs):
        return None

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    await _execute(owner, seed["run_id"], "r22a-parent")
    async with owner() as db:
        root = await db.get(RuntimeTask, seed["run_id"])
        assert root.status == "completed", root.result_summary
        task = (await db.scalars(select(RuntimeTask).where(RuntimeTask.root_runtime_task_id == root.id,
            RuntimeTask.task_type == "delegation"))).one()
        assert task.parent_agent_id == seed["agent_id"] and task.child_agent_id == seed["child_agent_id"]
        assert task.root_user_id == seed["user_id"]
        assert not task.metadata_json.get("target_artifacts") and not task.metadata_json.get("target_artifact_path")
        task.status, task.claimed_by, task.claim_version, task.attempt_count = "running", "r22a-colleague", 1, 1
        task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        seed["child_id"], seed["child_session_id"] = task.id, UUID(task.child_session_id)
        await db.commit()
    child_client = kernel_cases.ScriptedLLMClient([
        _round("read_file", {"path": seed["file_path"]}, "child-file"),
        {"content": RESULT},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: child_client)
    fence = set_runtime_task_fence(task_id=seed["child_id"], worker_id="r22a-colleague", claim_version=1)
    try:
        assert await orchestrator.dispatch_persisted_async_delegation(seed["child_id"].hex, wait_for_completion=True)
    finally:
        reset_runtime_task_fence(fence)
    calls = await _tool_results(owner, seed["child_id"])
    assert "Gamma,150" in calls["child-file"]["content"], calls
    assert len(child_client.requests) == 2
    async with owner() as db:
        child = await db.get(RuntimeTask, seed["child_id"])
        assert child.status == "completed" and child.result_summary == RESULT, child.result_summary
        call = (await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == child.id))).one()
        assert call.effect_state == "effect_committed" and call.tool_name == "read_file"
        pointer = calls["child-file"]["metadata"]["workspace_input_receipt"]
        assert pointer["content_input_receipt_id"] == str(seed["file_receipt_id"])
        seed["read_invocation_id"] = call.id
    if before_handback:
        await before_handback(seed)
    processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_factory, data_root=seed["data_root"])
    drained = await RuntimeTerminalBoundaryOutboxService(session_factory=app_factory).drain_once(
        tenant_id=seed["tenant_id"], worker_id="r22a-terminal", canonical_validator=processor.validate,
        process_callback=processor, task_types=("delegation",))
    assert drained["delivered"] == 1, drained
    async with owner() as db:
        notice = (await db.scalars(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == str(seed["child_id"])))).one()
        seed["result_ref"], seed["result_id"] = notice.result_ref, notice.result_object_id
        result = await db.get(RuntimeResultObject, notice.result_object_id)
        assert json.loads(bytes(result.payload_bytes))["artifacts"] == []
        events = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == seed["session_id"], ChatTranscriptEvent.run_id == seed["child_id"],
            ChatTranscriptEvent.event_type == "child_session")))
        assert len(events) == 1 and RESULT in events[0].content
    seed["child_client"] = child_client
    return seed


async def _read(seed, *, user_id=None, agent_id=None, session_id=None):
    from app.tools.handlers.context_resources import read_runtime_result
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    return json.loads(await read_runtime_result(ToolExecutionRequest(tool_name="read_runtime_result",
        arguments={"result_ref": seed["result_ref"]}, context=ToolExecutionContext(
            agent_id=agent_id or seed["agent_id"], user_id=user_id or seed["user_id"],
            tenant_id=str(seed["tenant_id"]), session_id=str(session_id or seed["session_id"]),
            runtime_task_id=str(seed["run_id"]), workspace=seed["agent_root"] / "workspace"))))


async def test_colleague_reads_own_uploaded_file_without_returning_an_artifact(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.services.runtime_result_sources import load_canonical_result_sources

    seed = await _file_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        result = await db.get(RuntimeResultObject, seed["result_id"])
        task, sources, returned_receipts = await load_canonical_result_sources(db, result)
        assert not returned_receipts and task.id == seed["child_id"]
        assert any(ref["source_kind"] == "workspace_artifact" and ref["source_id"] == str(seed["file_receipt_id"])
            for ref in sources), sources
        assert any(ref["source_kind"] == "session_tool_invocation" and ref["source_id"] == str(seed["read_invocation_id"])
            for ref in sources), sources
        expected_content = bytes(result.payload_bytes).decode(result.encoding)
    page = await _read(seed)
    print("R22A_NO_ARTIFACT_READ", json.dumps(page, ensure_ascii=False))
    assert page["status"] == "ok", page
    assert page["complete"] is True and page["dependency_coverage"] == {"complete": True}
    assert page["content"] == expected_content and RESULT in page["content"]


@pytest.mark.parametrize("reader", ["not_requester", "other_members_work"])
async def test_result_stays_bound_to_requesting_member_and_parent_work(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, reader,
):
    seed = await _file_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    agent_id, session_id = seed["agent_id"], seed["session_id"]
    if reader == "other_members_work":
        agent_id, session_id = uuid4(), uuid4()
        async with owner_sessionmaker() as db:
            db.add(Agent(id=agent_id, tenant_id=seed["tenant_id"], name="Other member's parent",
                creator_id=seed["other_user_id"], owner_user_id=seed["other_user_id"]))
            await db.flush()
            db.add(ChatSession(id=session_id, tenant_id=seed["tenant_id"], agent_id=agent_id,
                user_id=seed["other_user_id"], audience_kind="private", source_channel="web"))
            await db.commit()
    denied = await _read(seed, user_id=seed["other_user_id"], agent_id=agent_id, session_id=session_id)
    assert denied["status"] == "authority_denied" and denied["retryable"] is False, denied
    assert RESULT not in json.dumps(denied) and "Gamma,150" not in json.dumps(denied)


async def _revoke_source(owner, seed):
    async with owner() as db:
        (await db.get(KnowledgeGrant, seed["grant_id"])).revoked_at = datetime.now(timezone.utc)
        await db.commit()


async def test_published_result_refuses_a_later_revoked_source(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    seed = await _file_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, source_grant=True)
    readable = await _read(seed)
    assert readable["status"] == "ok" and RESULT in readable["content"], readable
    await _revoke_source(owner_sessionmaker, seed)
    denied = await _read(seed)
    print("R22A_REVOKED_ACTIVE_READ", json.dumps(denied, ensure_ascii=False))
    assert denied["status"] == "source_access_denied" and denied["reason_code"] == "content_source_access_denied", denied
    assert RESULT not in json.dumps(denied) and "Gamma,150" not in json.dumps(denied)


async def test_child_file_receipt_keeps_the_members_current_source_grant(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.core.source_read_purpose import source_recipient_read
    from app.services.content_input_receipts import ContentInputReceiptError, latest_workspace_receipt, validate_browser_input_dependencies

    async def remove_grant(seed):
        async with owner_sessionmaker() as db:
            await db.delete(await db.get(KnowledgeGrant, seed["grant_id"]))
            await db.commit()

    seed = await _file_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        source_grant=True, before_handback=remove_grant)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        receipt = await latest_workspace_receipt(db, seed["tenant_id"], seed["child_agent_id"], seed["file_path"])
        assert receipt.id == seed["file_receipt_id"]
        source = next(ref for ref in receipt.dependencies_json if ref["source_kind"] == "knowledge_document")
        assert source["source_id"] == str(seed["document_id"])
        with source_recipient_read(session_id=seed["session_id"], user_id=seed["user_id"]):
            with pytest.raises(ContentInputReceiptError) as denied_source:
                await validate_browser_input_dependencies(db, seed["tenant_id"], await db.get(User, seed["user_id"]), [source])
        assert denied_source.value.code == "content_source_access_denied" and denied_source.value.status_code == 403
    denied = await _read(seed)
    print("R22A_UNREADABLE_RECEIPT_SOURCE", json.dumps(denied, ensure_ascii=False))
    assert denied["status"] == "source_access_denied" and denied["reason_code"] == "content_source_access_denied", denied
    assert RESULT not in json.dumps(denied) and "Gamma,150" not in json.dumps(denied)


async def _deliver_continuation(owner, app_factory, seed):
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService

    service = RuntimeNotificationOutboxService(session_factory=app_factory)
    claimed = await service.claim_batch(worker_id="r22a-return", limit=100)
    pages = await service.prepare_integration_pages(worker_id="r22a-return",
        claimed=[item for item in claimed if item.tenant_id == seed["tenant_id"]])
    assert len(pages) == 1
    receipt = await service._deliver_page(pages[0])
    assert receipt.get("ok") is True, receipt
    await service._mark_page_delivered(page=pages[0], worker_id="r22a-return", receipt=receipt)
    async with owner() as db:
        continuation = (await db.scalars(select(RuntimeTask).where(RuntimeTask.parent_agent_id == seed["agent_id"],
            RuntimeTask.metadata_json["integration_page_id"].astext == str(pages[0].id)))).one()
        return continuation.id


async def _parent_turn(owner, seed, monkeypatch, script):
    import app.runtime.invoker as invoker
    from app.services.web_chat_runtime import start_web_chat_run

    async with owner() as db:
        queued = await start_web_chat_run(db=db, agent=await db.get(Agent, seed["agent_id"]),
            user=await db.get(User, seed["user_id"]), session=await db.get(ChatSession, seed["session_id"]),
            content="Continue with a short follow-up.", extra_metadata={"source": "websocket_message"})
        run_id = UUID(queued["run_id"])
        await db.commit()
    client = kernel_cases.ScriptedLLMClient(script)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    await _execute(owner, run_id, "r22a-follow-up")
    assert len(client.requests) == len(script)
    return run_id, client


async def test_revoked_handback_summary_is_not_copied_into_continuation_or_later_model_input(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import app.runtime.invoker as invoker

    async def revoke_before_handback(seed):
        await _revoke_source(owner_sessionmaker, seed)

    seed = await _file_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        source_grant=True, before_handback=revoke_before_handback)
    return_id = await _deliver_continuation(owner_sessionmaker, app_user_sessionmaker, seed)
    client = kernel_cases.ScriptedLLMClient([{"content": "The colleague finished; its result still needs an authorized read."}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    await _execute(owner_sessionmaker, return_id, "r22a-continuation")
    assert len(client.requests) == 1
    later_id, later = await _parent_turn(owner_sessionmaker, seed, monkeypatch, [{"content": "Please provide authorized evidence."}])
    for name, run_id, requests in [("continuation", return_id, client.requests), ("later", later_id, later.requests)]:
        wire = json.dumps(requests, ensure_ascii=False)
        print("R22A_REF_ONLY_MODEL_INPUT", name, "HAS_SUMMARY", RESULT in wire)
        assert RESULT not in wire and "Gamma,150" not in wire
        async with owner_sessionmaker() as db:
            models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id)))
            assert models
            for model in models:
                snapshot = model.model_request_snapshot_json
                assert RESULT not in json.dumps(snapshot["wire_request"])
                assert snapshot["input_bindings"]["dependency_coverage"]["complete"] is True
    denied = await _read(seed)
    assert denied["status"] == "source_access_denied", denied


async def test_model_history_rechecks_a_previously_read_delegated_result_after_revocation(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import app.runtime.invoker as invoker

    seed = await _file_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, source_grant=True)
    return_id = await _deliver_continuation(owner_sessionmaker, app_user_sessionmaker, seed)
    client = kernel_cases.ScriptedLLMClient([
        _round("read_runtime_result", {"result_ref": seed["result_ref"]}, "parent-file-result"),
        {"content": RESULT},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    await _execute(owner_sessionmaker, return_id, "r22a-consuming-parent")
    assert len(client.requests) == 2
    read = (await _tool_results(owner_sessionmaker, return_id))["parent-file-result"]
    page = json.loads(read["content"])
    assert page["status"] == "ok" and RESULT in page["content"] and page["dependency_coverage"] == {"complete": True}, page
    tool_message = next(message for message in client.requests[1]["messages"]
        if message.get("tool_call_id") == "parent-file-result")
    assert tool_message["content"] == read.get("model_visible_content", read["content"])
    assert json.loads(tool_message["content"]) == page
    await _revoke_source(owner_sessionmaker, seed)
    later_id, later = await _parent_turn(owner_sessionmaker, seed, monkeypatch, [{"content": "I need an authorized source to continue."}])
    wire = json.dumps(later.requests, ensure_ascii=False)
    print("R22A_REVOKED_HISTORY_MODEL_INPUT", "HAS_SUMMARY", RESULT in wire)
    async with owner_sessionmaker() as db:
        model = (await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == later_id))).one()
        bindings = model.model_request_snapshot_json["input_bindings"]
        print("R22A_REVOKED_HISTORY_BINDINGS", json.dumps(bindings, ensure_ascii=False))
    assert RESULT not in wire and "Gamma,150" not in wire
    assert any(entry["status"] == "withheld" and entry["code"] == "content_source_access_denied"
        for entry in bindings["messages"]), bindings
    assert bindings["dependency_coverage"] == {"complete": True}
