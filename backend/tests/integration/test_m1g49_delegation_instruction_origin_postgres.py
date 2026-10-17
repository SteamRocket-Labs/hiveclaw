"""Trace a delegated instruction to its actual canonical parent tool call."""

import json
import platform
import sys
from uuid import uuid4

import pytest

from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = [pytest.mark.usefixtures("migrated_pg_url"), pytest.mark.skipif(platform.system() != "Darwin",
    reason="Native macOS context gate; Linux delivery is deferred.")]


@pytest.mark.parametrize("input_case", ["complete", "unbound", "revoked"])
async def test_governed_delegation_preserves_its_parent_instruction_origin(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, input_case,
):
    import app.database as database
    from app.agents import orchestrator
    from app.config import get_settings
    from app.database import pin_rls_tenant_context
    from app.kernel.engine import _context_message
    from app.models.agent import Agent
    from app.models.capability_policy import CapabilityPolicy
    from app.models.llm import LLMModel
    from app.services.agent_manager import AgentManager
    from app.runtime.ccplus_contracts import permission_profile_snapshot
    from app.services import agent_tools, session_model_round
    from app.services.secrets_provider import get_secrets_provider
    from app.services.web_chat_runtime import _persist_tool_call

    parent_complete = input_case != "unbound"
    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="Delegate the authorized report to the worker.")
    receiver = uuid4()
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    async with owner_sessionmaker() as db:
        parent = await db.get(Agent, seed["agent_id"])
        await AgentManager().initialize_agent_files(db, parent, personality="NATIVE_PARENT_CONTEXT",
            original_author_id=seed["user_id"])
        model = await db.get(LLMModel, parent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-local-provider-only")
        model.max_input_tokens = 256_000
        db.add(Agent(id=receiver, tenant_id=seed["tenant_id"], name="Origin worker", creator_id=seed["user_id"],
            owner_user_id=seed["user_id"], primary_model_id=model.id))
        db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="agent.message.send", allowed=True,
            requires_approval=False))
        db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="workspace.file.write", allowed=True,
            requires_approval=False))
        await db.commit()
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    arguments = {"agent_name": "Origin worker", "target_agent_id": str(receiver),
        "message": "Produce the authorized report."}
    scope = {"tenant_id": seed["tenant_id"], "agent_id": seed["agent_id"], "session_id": seed["session_id"],
        "run_id": seed["run_id"], "turn_id": f"turn-{seed['run_id'].hex}", "round_index": 1}
    call_id = "parent-delegation-call"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        inputs = await session_model_round.bind_round_inputs(db, **scope)
        from app.kernel.engine import _mid_run_items_to_user_messages
        messages = [_context_message(role="system", content="Use the authorized tools.", kind="platform_guidance",
            unbound=() if parent_complete else ("controlled_parent_source_unbound",)),
            *_mid_run_items_to_user_messages(inputs)]
        provider_id = await session_model_round.prepare_model_request(db, **scope, messages=messages,
            tools=[], provider="openai", model="fake-4.1")
        await session_model_round.seal_model_response(db, **scope, provider_request_id=provider_id,
            response={"content": "", "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": "delegate_to_agent", "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"})
        await session_model_round.commit_sealed_model_round(db, **scope, provider_request_id=provider_id)
        await db.commit()
    event = {"name": "delegate_to_agent", "args": arguments, "tool_call_id": call_id,
        "provider_request_id": provider_id, "runtime_task_id": str(seed["run_id"])}
    await _persist_tool_call(agent_id=seed["agent_id"], user_id=seed["user_id"], session_id=str(seed["session_id"]),
        data={**event, "status": "running"})

    queued = {}
    async def capture_dispatch(**kwargs):
        queued.update(kwargs)
        queued["trace_id"] = kwargs.get("trace_id") or "origin-probe"
        handle = orchestrator.AsyncDelegationHandle(task_id=uuid4().hex, trace_id="origin-probe", target_name="Origin worker", status="queued")
        queued["task_id"] = handle.task_id
        return handle
    monkeypatch.setattr(orchestrator, "delegate_async", capture_dispatch)
    async def pre_effect(_payload):
        await _persist_tool_call(agent_id=seed["agent_id"], user_id=seed["user_id"], session_id=str(seed["session_id"]),
            data={**event, "status": "effect_started"})
    result = await agent_tools.execute_tool("delegate_to_agent", arguments, agent_id=seed["agent_id"],
        user_id=seed["user_id"], session_id=str(seed["session_id"]), runtime_task_id=str(seed["run_id"]),
        turn_id=scope["turn_id"], tool_call_id=call_id, pre_effect_callback=pre_effect,
        permission_profile=permission_profile_snapshot(None), emit_runtime_hooks=False)
    assert queued, str(result)
    message = queued["conversation_messages"][0]
    assert message["content"] == arguments["message"]
    assert isinstance(message.get("a2a_instruction_origin"), dict), message

    from dataclasses import replace
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from uuid import UUID
    from sqlalchemy import select
    import app.runtime.invoker as invoker
    from app.kernel.contracts import ExecutionIdentityRef
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionModelResult
    from app.services import agent_context, agent_manager
    from app.services.delegation_input_origin import load_delegation_instruction_origin
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    monkeypatch.setattr(agent_manager, "settings", get_settings())
    task_id = UUID(queued.pop("task_id"))
    request = orchestrator.AgentDelegationRequest(**queued, runtime_task_id=str(task_id),
        execution_identity=ExecutionIdentityRef(identity_type="delegated_user", identity_id=seed["user_id"]))
    request.execution_receipt = orchestrator._build_delegation_execution_receipt(request,
        task_id=str(task_id), trace_id="origin-probe", status="running")
    async with owner_sessionmaker() as db:
        target = await db.get(Agent, receiver)
        await AgentManager().initialize_agent_files(db, target, personality="NATIVE_RECEIVER_CONTEXT",
            original_author_id=seed["user_id"])
        db.add(RuntimeTask(id=task_id, task_type="delegation", status="running", tenant_id=seed["tenant_id"],
            parent_agent_id=seed["agent_id"], child_agent_id=receiver, parent_session_id=str(seed["session_id"]),
            child_session_id=request.session_id, root_session_id=str(seed["session_id"]), root_user_id=seed["user_id"],
            root_runtime_task_id=seed["run_id"], claimed_by="origin-worker", claim_version=1, attempt_count=1,
            claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            metadata_json=orchestrator._build_runtime_task_metadata(request, task_id=str(task_id))))
        if input_case == "revoked":
            parent = await db.get(Agent, seed["agent_id"])
            parent.deactivated_at = datetime.now(timezone.utc)
        await db.commit()
    report_path = "workspace/delegated-report.md"
    client = ScriptedLLMClient([{"tool_calls": [{"id": "child-report-write", "type": "function", "function": {
        "name": "write_file", "arguments": json.dumps({"path": report_path, "content": "DELEGATED_REPORT"})}}],
        "finish_reason": "tool_calls"}, {"content": "Report generated.", "finish_reason": "stop"}])
    from app.services import content_input_receipts
    native_writer = content_input_receipts.record_native_workspace_receipts
    observed = {}
    async def observe_writer(*args, **kwargs):
        observed["context"] = kwargs["context"]
        result = await native_writer(*args, **kwargs)
        observed["status"] = result
        return result
    monkeypatch.setattr(content_input_receipts, "record_native_workspace_receipts", observe_writer)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    token = set_runtime_task_fence(task_id=task_id, worker_id="origin-worker", claim_version=1)
    try:
        result = await orchestrator._delegate_after_cycle_check(request, trace_id=request.trace_id or "origin-probe",
            child_session_id=request.session_id, tool_profile=orchestrator._resolve_delegation_tool_profile(request.policy.tool_profile),
            is_delegation=True)
    finally:
        reset_runtime_task_fence(token)
    if input_case != "complete":
        expected = "delegation_instruction_sources_unbound" if input_case == "unbound" else "delegation_instruction_source_access_denied"
        assert result.failed and result.terminal_reason == expected, result
        assert result.parts[0]["status"] == ("unavailable" if input_case == "unbound" else "denied")
        assert not client.requests and not (seed["data_root"] / str(receiver) / report_path).exists()
        from app.models.chat_transcript_event import ChatTranscriptEvent
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            origin = await load_delegation_instruction_origin(db, request=request, tenant_id=seed["tenant_id"],
                content=message["content"])
            assert origin is not None and bool(origin.unbound) is (not parent_complete) and origin.sources
            assert not list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == task_id)))
            assert not list(await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == task_id,
                ChatTranscriptEvent.event_type == "user_message")))
        return
    assert not result.failed and result.transcript_committed and len(client.requests) == 2, result
    (tmp_path / "native-child-tool-results.json").write_text(json.dumps(client.requests, indent=2, default=str))
    assert (seed["data_root"] / str(receiver) / report_path).exists(), [m for m in client.requests[-1]["messages"] if m.get("role") == "tool"]
    assert (seed["data_root"] / str(receiver) / report_path).read_text() == "DELEGATED_REPORT"
    artifact_id = None
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        origin = await load_delegation_instruction_origin(db, request=request, tenant_id=seed["tenant_id"],
            content=message["content"])
        assert origin is not None and bool(origin.unbound) is (not parent_complete)
        command = await db.scalar(select(SessionCommand).where(SessionCommand.session_id == seed["session_id"]))
        assert origin.sources == ({"source_kind": "session_command", "source_id": str(command.id),
            "source_version": command.request_hash},)
        child_model = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == task_id)
            .order_by(SessionModelResult.round_id))
        bindings = child_model.model_request_snapshot_json["input_bindings"]
        from app.models.invocation_span import InvocationSpan
        sources = list(await db.scalars(select(InvocationSpan).where(InvocationSpan.runtime_task_id == task_id,
            InvocationSpan.span_type == "kernel_context")))
        (tmp_path / "child-source-coverage.json").write_text(json.dumps([
            {"kind": row.metadata_json["context_origin"]["kind"],
                "coverage": row.metadata_json["context_origin"]["dependency_coverage"]} for row in sources], indent=2))
        (tmp_path / "delegated-input-bindings.json").write_text(json.dumps(bindings, indent=2))
        assert bindings["status"] == "resolved" and all(entry["status"] == "bound" for entry in bindings["messages"]), bindings
        assert bindings["dependency_coverage"]["complete"] is parent_complete
        assert dict(origin.sources[0]) in bindings["dependencies"]
        from app.models.organization_identity import ContentInputReceipt
        receipt = await db.scalar(select(ContentInputReceipt).where(
            ContentInputReceipt.carrier_ref == f"{receiver}\n{report_path}"))
        (tmp_path / "native-receipt-status.json").write_text(json.dumps(observed.get("status"), indent=2, default=str))
        from app.services.content_input_receipts import ContentInputReceiptError, validate_runtime_input_dependencies
        from app.tools.runtime import ToolExecutionRequest
        errors = []
        if parent_complete:
            for dependency in bindings["dependencies"]:
                try:
                    await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                        context=observed["context"]), [dependency])
                except ContentInputReceiptError as exc:
                    errors.append({"source": dependency, "code": exc.code, "message": str(exc)})
        (tmp_path / "input-source-errors.json").write_text(json.dumps(errors, indent=2))
        assert (receipt is not None) is parent_complete, (observed.get("status"), errors)
        if receipt is not None:
            assert dict(origin.sources[0]) in receipt.dependencies_json
            from app.models.user import User
            from app.services.content_input_receipts import validate_browser_input_dependencies
            await validate_browser_input_dependencies(db, seed["tenant_id"], await db.get(User, seed["user_id"]),
                receipt.dependencies_json)
            context = observed["context"]
            source = dict(origin.sources[0])
            await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                context=context), [source])
            for altered in [replace(context, delegation_token=None), replace(context, session_id=str(uuid4())),
                replace(context, runtime_task_id=str(seed["run_id"])),
                replace(context, delegation_token=replace(context.delegation_token, expires_at=0))]:
                with pytest.raises(ContentInputReceiptError) as denied:
                    await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                        context=altered), [source])
                assert denied.value.status_code == 403 and denied.value.code == "content_source_access_denied"
            from app.services.content_input_receipts import _validate_input_dependencies
            from app.tools.handlers.knowledge import _personal_kb_runtime_principal
            principal = _personal_kb_runtime_principal(ToolExecutionRequest(tool_name="read_file", arguments={}, context=context))
            with pytest.raises(ContentInputReceiptError) as denied:
                await _validate_input_dependencies(db, seed["tenant_id"], [source], runtime_request=principal)
            assert denied.value.status_code == 403
            control = await db.begin_nested()
            bound_task = await db.get(RuntimeTask, task_id)
            bound_task.claim_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            await db.flush()
            with pytest.raises(ContentInputReceiptError) as denied:
                await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                    context=context), [source])
            assert denied.value.status_code == 403
            await control.rollback()
            from app.models.chat_artifact import ChatArtifact
            artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.agent_id == receiver,
                ChatArtifact.runtime_task_id == task_id, ChatArtifact.path == report_path))
            assert artifact is not None and artifact.snapshot_json["content_input_receipt_id"] == str(receipt.id)
            artifact_id = artifact.id
            from app.models.config_revision import ConfigRevision
            roster = next(ref for ref in receipt.dependencies_json if ref["source_id"].endswith("\na2a-collaborators"))
            control = await db.begin_nested()
            witness = await db.scalar(select(ConfigRevision).where(ConfigRevision.entity_id == seed["agent_id"],
                ConfigRevision.entity_type == "agent_bootstrap", ConfigRevision.version == 1))
            witness.changed_by_agent_id = receiver
            from sqlalchemy.exc import DBAPIError
            with pytest.raises(DBAPIError, match="config revision snapshots are immutable"):
                await db.flush()
            await control.rollback()
            control = await db.begin_nested()
            parent = await db.get(Agent, seed["agent_id"])
            parent.name = "Unwitnessed replacement label"
            await db.flush()
            with pytest.raises(ContentInputReceiptError) as denied:
                await validate_browser_input_dependencies(db, seed["tenant_id"], await db.get(User, seed["user_id"]), [roster])
            assert denied.value.status_code == 503
            await control.rollback()
        assert not list(await db.scalars(select(SessionCommand).where(SessionCommand.session_id == UUID(request.session_id))))
        for altered in [replace(request, owner_id=uuid4()), replace(request, parent_agent_id=uuid4()),
            replace(request, target=SimpleNamespace(id=uuid4()))]:
            assert await load_delegation_instruction_origin(db, request=altered, tenant_id=seed["tenant_id"],
                content=message["content"]) is None
        assert await load_delegation_instruction_origin(db, request=request, tenant_id=seed["tenant_id"],
            content="An unrelated instruction.") is None
        from copy import deepcopy
        forged = deepcopy(request.conversation_messages)
        forged[0]["a2a_instruction_origin"]["source_hash"] = "0" * 64
        assert await load_delegation_instruction_origin(db, request=replace(request, conversation_messages=forged),
            tenant_id=seed["tenant_id"], content=message["content"]) is None
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api import files
    from app.core.security import get_current_user
    from app.database import get_db
    async def viewer():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            return await db.get(User, seed["user_id"])
    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db
    app = FastAPI()
    app.include_router(files.router)
    app.dependency_overrides[get_current_user] = viewer
    app.dependency_overrides[get_db] = scoped_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as browser:
        response = await browser.get(f"/agents/{receiver}/files/artifacts/{artifact_id}/content")
        assert response.status_code == 200 and "DELEGATED_REPORT" in response.text, response.text
