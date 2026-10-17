"""Trace a delegated instruction to its actual canonical parent tool call."""

import json
import platform
import sys
from uuid import uuid4

import pytest

from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = [pytest.mark.usefixtures("migrated_pg_url"), pytest.mark.skipif(platform.system() != "Darwin",
    reason="Native macOS context gate; Linux delivery is deferred.")]


@pytest.mark.parametrize("input_case", ["complete", "unbound", "revoked", "first_task", "first_task_unbound", "first_task_revoked", "first_task_refined", "first_task_delivery", "first_task_parent_model", "first_task_refined_native", "first_task_native_creation"])
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

    first_task = input_case.startswith("first_task")
    native_creation = input_case == "first_task_native_creation"
    parent_model_case = input_case in {"first_task_parent_model", "first_task_refined_native", "first_task_native_creation"}
    delivery_case = input_case == "first_task_delivery" or parent_model_case
    native_refined = input_case in {"first_task_refined_native", "first_task_native_creation"}
    refined_input = input_case == "first_task_refined"
    unbound_input = input_case in {"unbound", "first_task_unbound"}
    revoked_input = input_case in {"revoked", "first_task_revoked"}
    parent_complete = not unbound_input
    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt=(
        "Create a research employee and have it produce the authorized report." if first_task
        else "Delegate the authorized report to the worker."))
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
        if first_task:
            from app.models.tenant_setting import TenantSetting

            db.add(TenantSetting(tenant_id=seed["tenant_id"], key="default_model_id", value={"model_id": str(model.id)}))
            db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="agent.employee.create", allowed=True,
                requires_approval=False))
        else:
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
    if first_task:
        from app.models.chat_session import ChatSession
        from app.models.runtime_task import RuntimeTask
        from app.models.session_v2 import SessionCommand, SessionTurnInput
        from app.models.user import User
        from app.services.session_input_admission import run_user_prompt_admission
        from app.services.session_input_dispatch import _start_input_runtime
        from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority

        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            prior = await db.get(RuntimeTask, seed["run_id"])
            prior.status = "completed"
            user = await db.get(User, seed["user_id"])
            authority = await resolve_session_mutation_authority(db, user=user, agent_id=seed["agent_id"],
                session_id=seed["session_id"], action="mutate_session_input")
            input_id = uuid4()
            await accept_human_input(db, authority=authority, intent={"kind": "start_turn", "input_id": str(input_id),
                "idempotency_key": str(input_id), "session_id": str(seed["session_id"]),
                "content_parts": [{"type": "text", "text": "Create a research employee and request the authorized report."}],
                "runtime_metadata": {"source": "session_input_api"}})
            await run_user_prompt_admission(db, authority=authority, input_id=input_id, worker_id="creation-origin-admission")
            row = await db.get(SessionTurnInput, input_id)
            creation_command_id = row.command_id
            started = await _start_input_runtime(db, row=row, command=await db.get(SessionCommand, row.command_id),
                agent=await db.get(Agent, seed["agent_id"]), user=user,
                session=await db.get(ChatSession, seed["session_id"]), successor=False)
            from uuid import UUID

            seed["run_id"], seed["turn_id"] = UUID(started["run_id"]), started["turn_id"]
            run = await db.get(RuntimeTask, seed["run_id"])
            run.status = "running"
            await db.commit()
    class SyntheticMemoryTransport:
        async def stream(self, **_kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(content=json.dumps({"summary": "Synthetic first-task session summary."}))
        async def close(self):
            pass
    instruction = "Produce the authorized report."
    tool_name = "create_employee_from_request" if first_task else "delegate_to_agent"
    arguments = ({"name": "Origin worker", "role_description": "Prepare referenced reports",
        "core_outputs": ["Report"], "boundaries": "Draft only; never send externally", "first_task": instruction}
        if first_task else {"agent_name": "Origin worker", "target_agent_id": str(receiver), "message": instruction})
    scope = {"tenant_id": seed["tenant_id"], "agent_id": seed["agent_id"], "session_id": seed["session_id"],
        "run_id": seed["run_id"], "turn_id": seed["turn_id"], "round_index": 1}
    call_id = "parent-delegation-call"
    if not native_creation:
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
                    "name": tool_name, "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"})
            await session_model_round.commit_sealed_model_round(db, **scope, provider_request_id=provider_id)
            await db.commit()
        event = {"name": tool_name, "args": arguments, "tool_call_id": call_id,
            "provider_request_id": provider_id, "runtime_task_id": str(seed["run_id"])}
        await _persist_tool_call(agent_id=seed["agent_id"], user_id=seed["user_id"], session_id=str(seed["session_id"]),
            data={**event, "status": "running"})
    queued = {}
    dispatches = []
    native_dispatch = orchestrator.delegate_async
    async def capture_dispatch(**kwargs):
        dispatches.append(kwargs)
        queued.update(kwargs)
        queued["trace_id"] = kwargs.get("trace_id") or "origin-probe"
        handle = (await native_dispatch(**kwargs) if first_task else orchestrator.AsyncDelegationHandle(
            task_id=uuid4().hex, trace_id="origin-probe", target_name="Origin worker", status="queued"))
        queued["task_id"] = handle.task_id
        return handle
    monkeypatch.setattr(orchestrator, "delegate_async", capture_dispatch)
    async def pre_effect(_payload):
        await _persist_tool_call(agent_id=seed["agent_id"], user_id=seed["user_id"], session_id=str(seed["session_id"]),
            data={**event, "status": "effect_started"})
    async def no_wakeup(**_kwargs):
        return None
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    if native_creation:
        from app.models.chat_transcript_event import ChatTranscriptEvent
        from app.models.session_v2 import SessionToolInvocation
        from app.services.session_semantic_history import _event_payload
        from app.services.web_chat_runtime import execute_web_chat_run

        creator_client = ScriptedLLMClient([
            {"tool_calls": [{"id": call_id, "type": "function", "function": {"name": tool_name,
                "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"},
            {"content": "The research employee and explicit first task are queued.", "finish_reason": "stop"}])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: creator_client)
        monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda *_args: SyntheticMemoryTransport())
        async with owner_sessionmaker() as db:
            run = await db.get(RuntimeTask, seed["run_id"])
            run.status, run.claimed_by, run.claim_version = "running", "native-creator-worker", 1
            run.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            await db.commit()
        creator_fence = set_runtime_task_fence(task_id=seed["run_id"], worker_id="native-creator-worker", claim_version=1)
        try:
            await execute_web_chat_run(seed["run_id"])
        finally:
            reset_runtime_task_fence(creator_fence)
        (tmp_path / "native-creator-model-requests.json").write_text(json.dumps(creator_client.requests, default=str, indent=2))
        assert len(creator_client.requests) == 2 and "NATIVE_PARENT_CONTEXT" in json.dumps(creator_client.requests[0]["messages"])
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            invocation = await db.scalar(select(SessionToolInvocation).where(SessionToolInvocation.run_id == seed["run_id"],
                SessionToolInvocation.provider_tool_use_id == call_id))
            assert invocation is not None and invocation.result_event_id is not None
            result = _event_payload(await db.get(ChatTranscriptEvent, invocation.result_event_id))["content"]
            from app.services.employee_creation_sources import creation_tool_result_bound

            assert (await db.get(RuntimeTask, seed["run_id"])).status == "completed"
            assert await creation_tool_result_bound(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                invocation=invocation, content=result)
            assert not await creation_tool_result_bound(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                invocation=invocation, content=json.dumps({**json.loads(result), "provisioning_task_id": str(uuid4())}))
            creating_rounds = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"])))
            assert len(creating_rounds) == 2 and all(row.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for row in creating_rounds)
    else:
        result = await agent_tools.execute_tool(tool_name, arguments, agent_id=seed["agent_id"],
            user_id=seed["user_id"], session_id=str(seed["session_id"]), runtime_task_id=str(seed["run_id"]),
            turn_id=scope["turn_id"], tool_call_id=call_id, pre_effect_callback=pre_effect,
            permission_profile=permission_profile_snapshot(None), emit_runtime_hooks=False)
    if first_task:
        from uuid import UUID
        from app.models.hr_creation import HrCreationDraft
        from app.models.runtime_task import RuntimeTask
        from app.services.hr_provisioning_runtime import execute_claimed_hr_provisioning

        creation = json.loads(str(result))
        assert creation.get("draft_status") == "confirmed", creation
        if delivery_case and not native_creation:
            await _persist_tool_call(agent_id=seed["agent_id"], user_id=seed["user_id"], session_id=str(seed["session_id"]),
                data={**event, "status": "done", "result": str(result)})
        job_id = UUID(creation["provisioning_task_id"])
        async with owner_sessionmaker() as db:
            job = await db.get(RuntimeTask, job_id)
            job.status, job.claimed_by, job.claim_version = "running", "creation-origin-worker", 1
            await db.commit()
        async def no_refinement(*_args, **_kwargs):
            if refined_input or native_refined:
                from types import SimpleNamespace

                return SimpleNamespace(provider="openai", model="synthetic-refiner", api_key="synthetic", base_url=None, max_output_tokens=17001), "synthetic_refiner"
            return None, "synthetic_transport"
        monkeypatch.setattr("app.tools.handlers.hr._resolve_employee_refinement_model", no_refinement)
        if refined_input:
            from app.tools.handlers.hr import _refine_soul_inputs

            async def same_bytes_without_model_io(**kwargs):
                return await _refine_soul_inputs(**{**kwargs, "model_config": {}})
            monkeypatch.setattr("app.tools.handlers.hr._refine_soul_inputs", same_bytes_without_model_io)
        refinement_calls = []
        if native_refined:
            async def refinement_transport(**kwargs):
                assert kwargs["usage_source"] == "hr_soul_refine"
                refinement_calls.append(kwargs)
                return {"choices": [{"message": {"content": json.dumps({
                    "personality": "REFINED_CONTEXT: Analyze sources carefully and deliver referenced draft reports.",
                    "quality_standards": ["Retain source references in each report."]})}}]}
            monkeypatch.setattr("app.services.llm_client.chat_complete", refinement_transport)
        monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda *_args: SyntheticMemoryTransport())
        assert await execute_claimed_hr_provisioning(job_id) == "completed"
        assert await execute_claimed_hr_provisioning(job_id) == "completed"
        if native_refined:
            assert len(refinement_calls) == 1 and "Prepare referenced reports" in refinement_calls[0]["messages"][0]["content"]
            assert refinement_calls[0]["max_tokens"] == 17001
        async with owner_sessionmaker() as db:
            draft = await db.get(HrCreationDraft, UUID(creation["blueprint_id"]))
            receiver = draft.created_agent_id
            assert draft.preview_json["creation_authorization"]["creation_origin"]
            if native_refined:
                assert "REFINED_CONTEXT" in (seed["data_root"] / str(receiver) / "soul.md").read_text()
    assert queued, str(result)
    assert len(dispatches) == 1
    message = queued["conversation_messages"][0]
    assert message["content"] == instruction
    assert isinstance(message.get("a2a_instruction_origin"), dict), message

    task_id = UUID(queued.pop("task_id"))
    if first_task:
        from app.services.runtime_task_service import get_runtime_task_record

        request = await orchestrator._build_delegation_request_from_runtime_record(await get_runtime_task_record(task_id.hex))
        assert request is not None and request.conversation_messages == [message]
    else:
        request = orchestrator.AgentDelegationRequest(**queued, runtime_task_id=str(task_id),
            execution_identity=ExecutionIdentityRef(identity_type="delegated_user", identity_id=seed["user_id"]))
        request.execution_receipt = orchestrator._build_delegation_execution_receipt(request,
            task_id=str(task_id), trace_id="origin-probe", status="running")
    async with owner_sessionmaker() as db:
        target = await db.get(Agent, receiver)
        if first_task:
            child = await db.get(RuntimeTask, task_id)
            child.status, child.claimed_by, child.claim_version, child.attempt_count = "running", "origin-worker", 1, 1
            child.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        else:
            await AgentManager().initialize_agent_files(db, target, personality="NATIVE_RECEIVER_CONTEXT",
                original_author_id=seed["user_id"])
            db.add(RuntimeTask(id=task_id, task_type="delegation", status="running", tenant_id=seed["tenant_id"],
            parent_agent_id=seed["agent_id"], child_agent_id=receiver, parent_session_id=str(seed["session_id"]),
            child_session_id=request.session_id, root_session_id=str(seed["session_id"]), root_user_id=seed["user_id"],
            root_runtime_task_id=seed["run_id"], claimed_by="origin-worker", claim_version=1, attempt_count=1,
            claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                metadata_json=orchestrator._build_runtime_task_metadata(request, task_id=str(task_id))))
        if revoked_input:
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
    if unbound_input or revoked_input:
        expected = "delegation_instruction_sources_unbound" if unbound_input else "delegation_instruction_source_access_denied"
        assert result.failed and result.terminal_reason == expected, result
        assert result.parts[0]["status"] == ("unavailable" if unbound_input else "denied")
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
        command = (await db.get(SessionCommand, creation_command_id) if first_task else
            await db.scalar(select(SessionCommand).where(SessionCommand.session_id == seed["session_id"])))
        command_source = {"source_kind": "session_command", "source_id": str(command.id), "source_version": command.request_hash}
        if native_creation:
            from app.services.session_model_inputs import _model_dependencies

            creating_model = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"],
                SessionModelResult.provider_request_id == invocation.provider_request_id))
            creating_sources, complete = _model_dependencies(creating_model)
            assert complete and list(origin.sources) == creating_sources and command_source in creating_sources
            assert {ref["source_kind"] for ref in creating_sources} >= {"agent_identity", "agent_context_resource", "session_actor_identity"}
        else:
            assert origin.sources == (command_source,)
        if first_task:
            from app.models.config_revision import ConfigRevision

            genesis = await db.scalar(select(ConfigRevision).where(ConfigRevision.entity_id == receiver,
                ConfigRevision.entity_type == "agent_hr_bootstrap"))
            assert genesis.changed_by_user_id is None and genesis.changed_by_agent_id == seed["agent_id"]
            assert genesis.content["dependencies"] == list(origin.sources)
            assert genesis.content["unbound"] == (["hr_refinement_sources_unbound"] if refined_input else [])
            if native_refined:
                captured = genesis.content["refinement"]["call"]
                assert captured["request"] == {key: refinement_calls[0][key] for key in captured["request"]}
                assert "REFINED_CONTEXT" in captured["response_content"] and "api_key" not in json.dumps(captured)
            assert not await db.scalar(select(ConfigRevision.id).where(ConfigRevision.entity_id == receiver,
                ConfigRevision.entity_type == "agent_bootstrap"))
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
        if refined_input:
            assert receipt is None and errors and all(error["code"] == "content_source_unavailable" for error in errors)
            assert observed["status"]["status"] == "unavailable"
            return
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
                for upstream_source in (origin.sources if native_creation else [source]):
                    with pytest.raises(ContentInputReceiptError) as denied:
                        await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                            context=altered), [upstream_source])
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
            roster = next(ref for ref in receipt.dependencies_json if ref["source_id"] == f"{receiver}\na2a-collaborators")
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
    if not delivery_case:
        return
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox

    marker = seed["data_root"] / str(receiver) / report_path
    marker.write_text("A later local edit must not replace the delivered version.")
    written_at = marker.stat().st_mtime_ns
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.direct_invocation_terminal_boundary_processor import _validate_response_payload
    from app.services.runtime_terminal_boundary_outbox import TerminalBoundaryCanonicalMismatch

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        child = await db.get(RuntimeTask, task_id)
        frontier = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == task_id)
            .order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
        with pytest.raises(TerminalBoundaryCanonicalMismatch):
            await _validate_response_payload(db=db, task=child, agent_id=receiver, session_id=request.session_id,
                source="agent", terminal_event=frontier, payload={**result.response_complete_payload,
                    "metadata": {**result.response_complete_payload["metadata"], "final_response": "Forged final."}})
    fence = set_runtime_task_fence(task_id=task_id, worker_id="origin-worker", claim_version=1)
    try:
        assert await orchestrator.dispatch_persisted_async_delegation(task_id.hex, wait_for_completion=True)
    finally:
        reset_runtime_task_fence(fence)
    assert len(client.requests) == 2 and marker.stat().st_mtime_ns == written_at
    processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=seed["data_root"])
    boundaries = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
        tenant_id=seed["tenant_id"], worker_id="native-first-task-terminal", canonical_validator=processor.validate,
        process_callback=processor, task_types=("delegation",))
    assert boundaries["delivered"] == 1, boundaries
    async with owner_sessionmaker() as db:
        child = await db.get(RuntimeTask, task_id)
        assert child.status == "completed", child.result_summary
        source_run = await db.get(RuntimeTask, seed["run_id"])
        source_run.status = "completed"
        await db.commit()
    service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
    claimed = await service.claim_batch(worker_id="native-first-task-return", limit=100)
    pages = await service.prepare_integration_pages(worker_id="native-first-task-return",
        claimed=[item for item in claimed if item.tenant_id == seed["tenant_id"]])
    assert pages
    for page in pages:
        delivered_page = await service._deliver_page(page)
        assert delivered_page.get("ok") is True
        repeated_page = await service._deliver_page(page)
        assert repeated_page["status"] == "already_delivered" and repeated_page["deduplicated"] is True
        assert repeated_page["event_id"] == delivered_page["event_id"]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        notification = await db.scalar(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(task_id),
            RuntimeNotificationOutbox.source_kind == "a2a_delegation"))
        assert notification is not None and notification.parent_session_id == seed["session_id"]
        returns = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.parent_agent_id == seed["agent_id"],
            RuntimeTask.metadata_json["integration_page_id"].astext.in_([str(page.id) for page in pages]))))
        assert len(returns) == 1 and returns[0].parent_session_id == str(seed["session_id"])
        return_id = returns[0].id
        result_ref = notification.result_ref
        from app.services.session_semantic_history import load_session_semantic_history

        history = await load_session_semantic_history(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            session_id=seed["session_id"], current_run_id=return_id)
        assert "DELEGATED_REPORT" not in json.dumps([item.content for item in history.messages])
    async def read_return():
        return json.loads(str(await agent_tools.execute_tool("read_runtime_result", {"result_ref": result_ref},
            agent_id=seed["agent_id"], user_id=seed["user_id"], session_id=str(seed["session_id"]),
            runtime_task_id=str(return_id), permission_profile=permission_profile_snapshot(None), emit_runtime_hooks=False)))
    delivered = await read_return()
    (tmp_path / "native-parent-result-read.json").write_text(json.dumps(delivered, indent=2))
    assert delivered["status"] == "ok" and "DELEGATED_REPORT" in delivered["content"], delivered
    assert "A later local edit" not in delivered["content"] and delivered["dependency_coverage"]["complete"] is True
    returned_artifact = json.loads(delivered["content"])["artifacts"][0]
    assert returned_artifact["artifact_id"] == str(artifact_id) and not returned_artifact["legacy_current_file_fallback"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as browser:
        returned = await browser.get(f"/agents/{returned_artifact['download_agent_id']}/files/artifacts/{returned_artifact['artifact_id']}/content")
        assert returned.status_code == 200 and "DELEGATED_REPORT" in returned.text and "A later local edit" not in returned.text
    if parent_model_case:
        from app.services.web_chat_runtime import execute_web_chat_run

        parent_path = "workspace/parent-review.md"
        parent_client = ScriptedLLMClient([
            {"tool_calls": [{"id": "parent-read-return", "type": "function", "function": {
                "name": "read_runtime_result", "arguments": json.dumps({"result_ref": result_ref})}}],
                "finish_reason": "tool_calls"},
            {"tool_calls": [{"id": "parent-write-review", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"path": parent_path,
                    "content": "ORIGINAL_WORK_REVIEW: DELEGATED_REPORT"})}}], "finish_reason": "tool_calls"},
            {"content": "Reviewed the returned report.\nDELIVERABLE: workspace/parent-review.md", "finish_reason": "stop"},
        ])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: parent_client)
        async with owner_sessionmaker() as db:
            continuation = await db.get(RuntimeTask, return_id)
            continuation.status, continuation.claimed_by, continuation.claim_version = "running", "parent-return-worker", 1
            continuation.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            await db.commit()
        parent_fence = set_runtime_task_fence(task_id=return_id, worker_id="parent-return-worker", claim_version=1)
        try:
            await execute_web_chat_run(return_id)
        finally:
            reset_runtime_task_fence(parent_fence)
        (tmp_path / "native-parent-model-requests.json").write_text(json.dumps(parent_client.requests, default=str, indent=2))
        assert len(parent_client.requests) == 3
        second_request = json.dumps(parent_client.requests[1]["messages"])
        assert "DELEGATED_REPORT" in second_request and "A later local edit" not in second_request
        assert (seed["agent_root"] / parent_path).read_text() == "ORIGINAL_WORK_REVIEW: DELEGATED_REPORT"
        assert observed["status"]["status"] == "captured", observed["status"]
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            continuation = await db.get(RuntimeTask, return_id)
            assert continuation.status == "completed", continuation.result_summary
            artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.agent_id == seed["agent_id"],
                ChatArtifact.runtime_task_id == return_id, ChatArtifact.path == parent_path))
            assert artifact is not None and artifact.snapshot_json.get("content_input_receipt_id"), artifact
            parent_artifact_id = artifact.id
            models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == return_id)))
            assert len(models) == 3 and all(model.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for model in models)
            parent_receipt = await db.get(ContentInputReceipt, UUID(artifact.snapshot_json["content_input_receipt_id"]))
            source = next(ref for ref in parent_receipt.dependencies_json if ref["source_kind"] == "runtime_result")
            await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                context=observed["context"]), [source])
            for altered_source, altered_context in [({**source, "source_version": "0" * 64}, observed["context"]),
                (source, replace(observed["context"], session_id=str(uuid4())))]:
                with pytest.raises(ContentInputReceiptError) as denied:
                    await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name="read_file", arguments={},
                        context=altered_context), [altered_source])
                assert denied.value.status_code == 403
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as browser:
            review = await browser.get(f"/agents/{seed['agent_id']}/files/artifacts/{parent_artifact_id}/content")
            assert review.status_code == 200 and review.json()["content"] == "ORIGINAL_WORK_REVIEW: DELEGATED_REPORT" and review.json()["uses_snapshot"], review.text

    from app.models.hr_creation import HrCreationDraft
    from copy import deepcopy

    if native_refined:
        from app.models.hr_creation import HrProvisioningStep
        from app.services.config_versioning import canonical_content_hash

        async with owner_sessionmaker() as db:
            step = await db.scalar(select(HrProvisioningStep).where(HrProvisioningStep.draft_id == UUID(creation["blueprint_id"]),
                HrProvisioningStep.step_key == "model"))
            original_receipt = deepcopy(step.receipt_json)
            altered_receipt = deepcopy(original_receipt)
            call = altered_receipt["refinement"]["call"]
            call["request"]["messages"][0]["content"] += " Forged extra source."
            call["sha256"] = canonical_content_hash({key: value for key, value in call.items() if key != "sha256"})
            step.receipt_json = altered_receipt
            await db.commit()
        rejected_refinement = await read_return()
        assert rejected_refinement["status"] == "source_unavailable" and "DELEGATED_REPORT" not in json.dumps(rejected_refinement)
        async with owner_sessionmaker() as db:
            step = await db.get(HrProvisioningStep, step.id)
            step.receipt_json = original_receipt
            await db.commit()
        assert (await read_return())["status"] == "ok" and len(refinement_calls) == 1
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, UUID(creation["blueprint_id"]))
        accepted = deepcopy(draft.preview_json)
        changed = deepcopy(accepted)
        changed["creation_authorization"]["creation_origin"]["source_hash"] = "0" * 64
        draft.preview_json = changed
        await db.commit()
    altered_binding = await read_return()
    assert altered_binding["status"] == "source_unavailable" and "DELEGATED_REPORT" not in json.dumps(altered_binding)
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, UUID(creation["blueprint_id"]))
        draft.preview_json = accepted
        await db.commit()
    assert (await read_return())["status"] == "ok"
    soul = seed["data_root"] / str(receiver) / "soul.md"
    soul.write_text(soul.read_text() + "\nChanged after first-task delivery.\n")
    stale = await read_return()
    assert stale["status"] == "source_access_denied" and stale["reason_code"] == "content_source_changed", stale
    assert "DELEGATED_REPORT" not in json.dumps(stale) and len(client.requests) == 2
    if parent_model_case:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as browser:
            stale_review = await browser.get(f"/agents/{seed['agent_id']}/files/artifacts/{parent_artifact_id}/content")
            assert stale_review.status_code == 403 and "DELEGATED_REPORT" not in stale_review.text
        assert len(parent_client.requests) == 3
