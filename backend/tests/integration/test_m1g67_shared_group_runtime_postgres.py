"""Run the real shared-work kernel with the original per-message principal."""

import json
import platform
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import select

from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run, ScriptedLLMClient

pytestmark = [pytest.mark.usefixtures("migrated_pg_url"), pytest.mark.skipif(platform.system() != "Darwin",
    reason="Native macOS context gate; Linux delivery is deferred.")]


@pytest.mark.parametrize("mutation", [None, "forged_actor", "unlinked", "private_source", "private_recipient", "unknown_recipient", "queued_member", "same_sender", "explicit_other", "private_context", "shared_context"])
async def test_shared_work_runs_with_its_original_sender_and_real_tool_sources(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, mutation,
):
    import app.database as database
    import app.runtime.invoker as invoker
    from app.config import get_settings
    from app.database import pin_rls_tenant_context
    from app.models.agent import Agent
    from app.models.audit import ChatMessage
    from app.models.channel_config import ChannelConfig
    from app.models.channel_ingress_event import ChannelIngressEvent
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_session import ChatSession
    from app.models.external_principal import ExternalPrincipal
    from app.models.invocation_span import InvocationSpan
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.security_audit import ResourcePermission
    from app.models.session_v2 import SessionCommand, SessionModelResult, SessionTurnInput
    from app.models.user import User
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.channel_ingress_context import use_channel_ingress_context, bind_channel_ingress_external_principal
    from app.services.channel_ingress_inbox import accept_authenticated_channel_event, ChannelIngressInboxService
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    from app.services.channel_delivery_service import DeliveryResult
    from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
    from app.services.channel_session import find_or_create_shared_group_session
    from app.services.external_principal_service import bind_authenticated_self_channel_principal
    from app.services.feishu_service import FeishuService
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.secrets_provider import get_secrets_provider
    from app.services.session_live_input import submit_live_human_input
    from app.services.session_input_dispatch import recover_admitted_session_inputs_once
    from app.services.web_chat_runtime import execute_web_chat_run

    private_canary = "PRIVATE_GROUP_NOTE_CANARY"
    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="PRIVATE_GROUP_SOURCE_CANARY")
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
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    monkeypatch.setattr(agent_manager, "settings", get_settings())
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        agent.default_session_permission_mode = "bypassPermissions"
        await AgentManager().initialize_agent_files(db, agent, personality="SHARED_EMPLOYEE_ROLE",
            original_author_id=seed["user_id"])
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-group-provider-only")
        model.max_input_tokens = 256_000
        db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="workspace.file.write", allowed=True,
            requires_approval=False))
        (await db.get(RuntimeTask, seed["run_id"])).status = "completed"
        member = User(id=uuid4(), tenant_id=seed["tenant_id"], username=f"group-{uuid4().hex}",
            email=f"{uuid4().hex}@group.test", password_hash="x", display_name="Group Requester")
        db.add(member)
        await db.flush()
        grant = ResourcePermission(tenant_id=seed["tenant_id"], principal_type="user", principal_id=member.id,
            resource_type="agent", resource_id=agent.id, actions=["read", "execute", "manage"], effect="allow")
        config = ChannelConfig(id=uuid4(), tenant_id=seed["tenant_id"], agent_id=agent.id, channel_type="feishu",
            app_id="synthetic-group-app", app_secret="synthetic-group-secret", is_configured=True)
        db.add_all([grant, config])
        await db.flush()
        bound = await bind_authenticated_self_channel_principal(db, tenant_id=seed["tenant_id"], config=config,
            provider_subject_id="ou_group_requester", user_id=member.id, actor_user_id=member.id)
        actor = bound.actor
        if mutation in {"private_recipient", "queued_member", "explicit_other", "private_context", "shared_context"}:
            other = await bind_authenticated_self_channel_principal(db, tenant_id=seed["tenant_id"], config=config,
                provider_subject_id="ou_group_creator", user_id=seed["user_id"], actor_user_id=seed["user_id"])
        grant.actions = ["read", "execute"]
        shared = await find_or_create_shared_group_session(db, tenant_id=seed["tenant_id"], agent_id=agent.id,
            installation_ref=str(config.id), chat_id="oc_native_group", first_message_title="Shared report")
        shared_id, installation = shared.id, str(config.id)
        if mutation in {"private_context", "shared_context"}:
            import hashlib
            from app.services.workspace_resource_authority import register_workspace_path
            notes = seed["agent_root"] / "workspace/private-notes.md"
            notes.write_text(private_canary)
            await register_workspace_path(db, tenant_id=seed["tenant_id"], agent_id=agent.id,
                path="workspace/private-notes.md", owner_user_id=member.id, root_session_id=shared_id,
                source="test_private_input", content_hash=hashlib.sha256(notes.read_bytes()).hexdigest())
            from app.services.content_input_receipts import record_workspace_receipt
            record_workspace_receipt(db, seed["tenant_id"], agent.id, "workspace/private-notes.md",
                hashlib.sha256(notes.read_bytes()).hexdigest(), None)
            if mutation == "shared_context":
                from app.core.resource_authority import workspace_resource_id
                for path in ("workspace/private-notes.md", "workspace/shared-report.md"):
                    permission = ResourcePermission(id=uuid4(), tenant_id=seed["tenant_id"], principal_type="user",
                        principal_id=seed["user_id"], resource_type="workspace_file",
                        resource_id=workspace_resource_id(agent.id, path), actions=["read"], effect="allow", conditions={})
                    db.add(permission)
                    if path == "workspace/private-notes.md":
                        note_grant_id = permission.id
        await db.commit()

    async def members(_self, *_args, **_kwargs):
        subjects = {"ou_group_requester"}
        if mutation in {"private_recipient", "unknown_recipient", "queued_member", "explicit_other", "private_context", "shared_context"}:
            subjects.add("ou_group_unknown" if mutation == "unknown_recipient" else "ou_group_creator")
        return subjects
    monkeypatch.setattr(FeishuService, "get_chat_member_open_ids", members)
    async def no_wakeup(**_kwargs):
        return None
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    class MemoryTransport:
        async def stream(self, **_kwargs):
            return SimpleNamespace(content=json.dumps({"summary": "Synthetic group summary."}))
        async def close(self):
            pass
    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda *_args: MemoryTransport())
    report_content = private_canary if mutation in {"private_context", "shared_context"} else "GROUP_REPORT"
    prompt = ("Read my private workspace/private-notes.md, write workspace/shared-report.md from it, and read it back."
        if mutation in {"private_context", "shared_context"} else "Write workspace/shared-report.md with GROUP_REPORT, read it back, and report.")
    async def submit(sender, subject, content, key, **intent):
        body = {"event": {"sender": {"sender_id": {"open_id": subject}}, "message": {
            "chat_type": "group", "chat_id": "oc_native_group", "message_type": "text", "content": json.dumps({"text": content})}}}
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            event = await accept_authenticated_channel_event(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                provider="feishu", installation_ref=installation, provider_event_id=key,
                handler_key="feishu.event", body=body)
        assert any(item.id == event.event_id for item in await ChannelIngressInboxService(
            session_factory=app_user_sessionmaker).claim_batch(worker_id="shared-ingress"))
        with use_channel_ingress_context(event_id=event.event_id, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            provider="feishu", installation_ref=installation):
            bind_channel_ingress_external_principal(sender.external_principal_id)
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                session = await db.get(ChatSession, shared_id)
                db.add(ChatMessage(tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], user_id=sender.id,
                    external_principal_id=sender.external_principal_id, conversation_id=str(shared_id), role="user", content=content))
                await db.flush()
                accepted = await submit_live_human_input(db=db, agent=await db.get(Agent, seed["agent_id"]), user=sender,
                    session=session, source="feishu", content=content, input_id=event.event_id, idempotency_key=key,
                    runtime_metadata={"source": "feishu", "channel": "feishu"}, **intent)
                ingress = await db.get(ChannelIngressEvent, event.event_id)
                ingress.status, ingress.external_principal_id = "processed", sender.external_principal_id
                await db.commit()
        return event, accepted

    event, accepted = await submit(actor, "ou_group_requester", prompt, "native-shared-work")
    command_id = UUID(accepted["command_id"])
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        await recover_admitted_session_inputs_once(db, worker_id="shared-dispatch", tenant_id=seed["tenant_id"],
            admission_ids=(uuid5(command_id, "input-admission:revision:1"),))
        task = await db.scalar(select(RuntimeTask).where(RuntimeTask.metadata_json["session_v2_input_id"].astext == str(event.event_id)))
        assert task is not None, accepted
        run_id = task.id
        assert task.root_user_id == member.id != seed["user_id"]
        assert (await db.get(SessionCommand, command_id)).principal_id == actor.external_principal_id
        if mutation not in {"queued_member", "same_sender", "explicit_other", "private_context", "shared_context"}:
            task.status, task.claimed_by, task.claim_version, task.attempt_count = "running", "shared-worker", 1, 1
            task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        if mutation == "forged_actor":
            task.root_user_id = seed["user_id"]
            task.metadata_json = {**task.metadata_json, "user_id": str(seed["user_id"])}
        elif mutation == "unlinked":
            (await db.get(ExternalPrincipal, actor.external_principal_id)).linked_user_id = None
        elif mutation == "private_source":
            import app.services.session_model_inputs as model_inputs
            private = await db.scalar(select(SessionCommand).where(SessionCommand.session_id == seed["session_id"],
                SessionCommand.namespace == "human_input"))
            assert private is not None
            original_bindings = model_inputs.build_model_input_bindings
            async def with_private_source(*args, **kwargs):
                manifest = await original_bindings(*args, **kwargs)
                manifest["dependencies"].append({"source_kind": "session_command", "source_id": str(private.id),
                    "source_version": private.request_hash})
                return manifest
            monkeypatch.setattr(model_inputs, "build_model_input_bindings", with_private_source)
        await db.commit()
    second_event = second_command_id = None
    other_content = ("OTHER_REQUESTER_ONLY: continue the authorized earlier report into workspace/next-report.md and read it back."
        if mutation == "shared_context" else "OTHER_REQUESTER_ONLY: write workspace/next-report.md with NEXT_GROUP_REPORT, read it back, and report."
        if mutation == "private_context" else "OTHER_REQUESTER_ONLY")
    if mutation in {"queued_member", "same_sender", "explicit_other", "private_context", "shared_context"}:
        from fastapi import HTTPException
        sender = actor if mutation == "same_sender" else other.actor
        subject = "ou_group_requester" if mutation == "same_sender" else "ou_group_creator"
        if mutation == "explicit_other":
            with pytest.raises(HTTPException) as conflict:
                await submit(sender, subject, "OTHER_REQUESTER_ONLY", "native-shared-second",
                    requested_kind="steer_current_turn", expected_run_id=run_id,
                    expected_turn_id=task.metadata_json["turn_id"])
            assert conflict.value.status_code == 409 and conflict.value.detail["code"] == "shared_group_requester_conflict"
        else:
            second_event, second = await submit(sender, subject,
                "SAME_REQUESTER_UPDATE" if mutation == "same_sender" else other_content, "native-shared-second")
            second_command_id = UUID(second["command_id"])
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            if second_command_id is not None:
                await recover_admitted_session_inputs_once(db, worker_id="shared-second-dispatch", tenant_id=seed["tenant_id"],
                    admission_ids=(uuid5(second_command_id, "input-admission:revision:1"),))
            task = await db.get(RuntimeTask, run_id)
            assert task.status == "pending" and task.root_user_id == member.id
            if second_event is not None:
                incoming = await db.get(SessionTurnInput, second_event.event_id)
                assert incoming.intent == ("steer_current_turn" if mutation == "same_sender" else "queue_next_turn")
                assert incoming.target_run_id == (run_id if mutation == "same_sender" else None)
                assert (await db.get(SessionCommand, second_command_id)).principal_id == sender.external_principal_id
                if mutation in {"queued_member", "private_context", "shared_context"}:
                    from app.services.session_semantic_history import load_session_semantic_history
                    prior = await load_session_semantic_history(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], session_id=shared_id)
                    assert "OTHER_REQUESTER_ONLY" not in json.dumps([message.content for message in prior.messages])
            else:
                assert await db.scalar(select(SessionCommand.id).where(SessionCommand.session_id == shared_id,
                    SessionCommand.idempotency_key == "native-shared-second")) is None
            task.status, task.claimed_by, task.claim_version, task.attempt_count = "running", "shared-worker", 1, 1
            task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            await db.commit()
    responses = [
        {"tool_calls": [{"id": "shared-write", "type": "function", "function": {"name": "write_file",
            "arguments": json.dumps({"path": "workspace/shared-report.md", "content": report_content})}}], "finish_reason": "tool_calls"},
        {"tool_calls": [{"id": "shared-read", "type": "function", "function": {"name": "read_file",
            "arguments": json.dumps({"path": "workspace/shared-report.md"})}}], "finish_reason": "tool_calls"},
        {"content": "The shared report is ready.", "finish_reason": "stop"},
    ]
    if mutation in {"private_context", "shared_context"}:
        responses.insert(0, {"tool_calls": [{"id": "private-read", "type": "function", "function": {"name": "read_file",
            "arguments": json.dumps({"path": "workspace/private-notes.md"})}}], "finish_reason": "tool_calls"})
        responses[-1]["content"] = f"The private report says {private_canary}."
    if mutation is None:
        responses[-1]["content"] = "The [shared report](workspace/shared-report.md) is ready."
    client = ScriptedLLMClient(responses)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    fence = set_runtime_task_fence(task_id=run_id, worker_id="shared-worker", claim_version=1)
    try:
        await execute_web_chat_run(run_id)
    finally:
        reset_runtime_task_fence(fence)
    (tmp_path / "shared-model-requests.json").write_text(json.dumps(client.requests, default=str, indent=2))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        task = await db.get(RuntimeTask, run_id)
        results = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id)))
        (tmp_path / "shared-model-bindings.json").write_text(json.dumps([row.model_request_snapshot_json for row in results], default=str, indent=2))
        shared = await db.get(ChatSession, shared_id)
        assert shared.user_id is None and shared.external_principal_id is None
        if mutation in {"forged_actor", "unlinked", "private_source"}:
            assert task.status != "completed", (task.status, task.result_summary)
            assert client.requests == []
            assert not (seed["agent_root"] / "workspace/shared-report.md").exists()
            return
        assert task.status == "completed", (task.status, task.result_summary, task.metadata_json)
        if mutation in {"queued_member", "private_context", "shared_context"}:
            assert "OTHER_REQUESTER_ONLY" not in json.dumps(client.requests)
        if mutation == "same_sender":
            assert "SAME_REQUESTER_UPDATE" in json.dumps(client.requests)
        assert len(client.requests) == (4 if mutation in {"private_context", "shared_context"} else 3) and report_content in json.dumps(client.requests[-1]["messages"])
        reads = [message for message in client.requests[-1]["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == "shared-read"]
        assert len(reads) == 1 and report_content in str(reads[0]["content"]), reads
        assert all(row.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for row in results)
        assert (await db.get(SessionTurnInput, event.event_id)).status == "applied"
        assert (seed["agent_root"] / "workspace/shared-report.md").read_text() == report_content
        from app.models.chat_artifact import ChatArtifact
        from app.models.chat_transcript_event import ChatTranscriptEvent
        from app.services.chat_artifact_delivery import captured_chat_artifact_file

        artifacts = list(await db.scalars(select(ChatArtifact).where(ChatArtifact.runtime_task_id == run_id)))
        assert len(artifacts) == 1, [(row.id, row.path) for row in artifacts]
        artifact = artifacts[0]
        assert artifact.owner_user_id == member.id and artifact.root_session_id == shared_id
        assert artifact.snapshot_json["tool_invocation_id"] and artifact.snapshot_json["execution_evidence_ref"]
        terminal_events = list(await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == run_id,
            ChatTranscriptEvent.item_kind == "assistant_final", ChatTranscriptEvent.lifecycle == "completed")))
        assert len(terminal_events) == 1 and str(artifact.id) in terminal_events[0].metadata_json["v2_payload"]["artifact_refs"]
        with captured_chat_artifact_file(artifact, seed["agent_root"]) as (captured, _source):
            assert captured is not None and captured.read_text() == report_content
        tool_events = list(await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == run_id,
            ChatTranscriptEvent.item_kind == "tool_result")))
        assert any(part.get("artifact_id") == str(artifact.id) for row in tool_events for part in row.parts_json or [])
        if mutation is None:
            from app.api import files
            from app.services.chat_artifact_delivery import create_chat_artifacts_for_message

            monkeypatch.setattr(files, "settings", get_settings())
            content = await files.read_artifact_content(agent_id=seed["agent_id"], artifact_id=artifact.id,
                current_user=await db.get(User, member.id), db=db, operator_view=False, operator_reason=None)
            assert content.content == report_content
            errors = []
            late = await create_chat_artifacts_for_message(db=db, agent_id=seed["agent_id"], tenant_id=seed["tenant_id"],
                session_id=shared_id, message_id=artifact.message_id, runtime_task_id=run_id, paths=[artifact.path],
                workspace_root=seed["agent_root"], owner_user_id=seed["user_id"], publication_errors=errors)
            assert late == [] and errors[0]["reason_code"] == "artifact_source_principal_required"
        spans = list(await db.scalars(select(InvocationSpan).where(InvocationSpan.runtime_task_id == run_id,
            InvocationSpan.span_type == "kernel_context")))
        assert spans and all(span.user_id == member.id for span in spans)
        dependencies = [source for result in results for source in result.model_request_snapshot_json["input_bindings"]["dependencies"]
            if source["source_kind"] == "session_tool_invocation"]
        assert dependencies
        await _validate_input_dependencies(db, seed["tenant_id"], dependencies, user=await db.get(User, member.id))
        if mutation in {"private_recipient", "queued_member", "explicit_other", "private_context"}:
            with pytest.raises(ContentInputReceiptError) as private:
                await _validate_input_dependencies(db, seed["tenant_id"], dependencies, user=await db.get(User, seed["user_id"]))
            assert private.value.status_code == 403
        outbox = list(await db.scalars(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == run_id)))
        assert len(outbox) == 1
        # Prioritize this fixture without claiming unrelated session-scoped test rows.
        outbox[0].available_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        await db.commit()
    sends = []
    async def send(**kwargs):
        sends.append(kwargs)
        return DeliveryResult(ok=True, status="delivered", channel="feishu", message="Synthetic provider receipt.")
    outcome = await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker,
        text_sender=send, file_sender=send).drain_once(worker_id="shared-delivery", limit=1)
    assert outcome["claimed"] == 1, outcome
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        delivery = await db.get(ChannelDeliveryOutbox, outbox[0].id)
        if mutation in {"private_recipient", "queued_member", "explicit_other", "private_context"}:
            assert outcome["dead_lettered"] == 1 and sends == [], outcome
            assert delivery.status == "dead_letter" and "shared_group_output_unavailable" in delivery.last_error
        elif mutation == "unknown_recipient":
            assert outcome["retried"] == 1 and sends == [], outcome
            assert delivery.status == "pending" and "group_recipient_identity_unavailable" in delivery.last_error
        else:
            assert outcome["delivered"] == 1 and len(sends) == 2, outcome
            assert sends[1]["artifact_id"] == artifact.id and sends[1]["file_path"].read_text() == report_content
            assert sends[0]["reply_target"]["receive_id"] == "oc_native_group"
            assert delivery.status == "delivered"
    if mutation in {"queued_member", "private_context", "shared_context"}:
        with use_channel_ingress_context(event_id=second_event.event_id, tenant_id=seed["tenant_id"],
            agent_id=seed["agent_id"], provider="feishu", installation_ref=installation):
            bind_channel_ingress_external_principal(other.actor.external_principal_id)
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                replay = await submit_live_human_input(db=db, agent=await db.get(Agent, seed["agent_id"]),
                    user=other.actor, session=await db.get(ChatSession, shared_id), source="feishu",
                    content=other_content, input_id=second_event.event_id, idempotency_key="native-shared-second",
                    runtime_metadata={"source": "feishu", "channel": "feishu"})
                assert replay["command_id"] == str(second_command_id)
                await db.commit()
        from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
        from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor
        processor = WebTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=seed["data_root"])
        boundary = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=seed["tenant_id"], worker_id="group-terminal-boundary", canonical_validator=processor.validate,
            process_callback=processor, task_types=("web_chat_turn",))
        assert boundary["delivered"] == 1, boundary
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            await recover_admitted_session_inputs_once(db, worker_id="group-next-requester", tenant_id=seed["tenant_id"],
                admission_ids=(uuid5(second_command_id, "input-admission:revision:1"),))
            successor = await db.scalar(select(RuntimeTask).where(RuntimeTask.id == uuid5(second_event.event_id, "session-v2-successor-run")))
            assert successor is not None and successor.root_user_id == seed["user_id"] != member.id
            assert successor.metadata_json["external_principal_id"] == str(other.actor.external_principal_id)
            assert (await db.get(SessionTurnInput, second_event.event_id)).target_run_id == successor.id
            successor.status, successor.claimed_by = "running", "group-next-requester"
            successor.claim_version, successor.attempt_count = 1, 1
            successor.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            from app.services.shared_group_authority import load_shared_group_runtime_actor
            restored = await load_shared_group_runtime_actor(db, task=successor, session=await db.get(ChatSession, shared_id))
            assert restored.id == seed["user_id"] and restored.external_principal_id == other.actor.external_principal_id
            await db.commit()
        if mutation in {"private_context", "shared_context"}:
            assert private_canary not in json.dumps(client.requests[0])
            next_content = f"{private_canary} continued" if mutation == "shared_context" else "NEXT_GROUP_REPORT"
            next_client = ScriptedLLMClient([
                {"tool_calls": [{"id": "next-write", "type": "function", "function": {"name": "write_file",
                    "arguments": json.dumps({"path": "workspace/next-report.md", "content": next_content})}}], "finish_reason": "tool_calls"},
                {"tool_calls": [{"id": "next-read", "type": "function", "function": {"name": "read_file",
                    "arguments": json.dumps({"path": "workspace/next-report.md"})}}], "finish_reason": "tool_calls"},
                {"content": "The independent report is ready.", "finish_reason": "stop"},
            ])
            monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: next_client)
            fence = set_runtime_task_fence(task_id=successor.id, worker_id="group-next-requester", claim_version=1)
            try:
                await execute_web_chat_run(successor.id)
            finally:
                reset_runtime_task_fence(fence)
            (tmp_path / "next-model-requests.json").write_text(json.dumps(next_client.requests, default=str, indent=2))
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                next_task = await db.get(RuntimeTask, successor.id)
                assert next_task.status == "completed", (next_task.status, next_task.result_summary, next_task.metadata_json)
                assert len(next_client.requests) == 3
                if mutation == "private_context":
                    assert private_canary not in json.dumps(next_client.requests)
                    holds = next_task.metadata_json["session_semantic_history"]["held_items"]
                    assert any(item["kind"] == "source_access_denied" and item["evidence_refs"] for item in holds)
                    assert private_canary not in json.dumps(holds)
                else:
                    previous_reads = [message for message in next_client.requests[0]["messages"]
                        if message.get("role") == "tool" and message.get("tool_call_id") == "private-read"]
                    assert len(previous_reads) == 1 and private_canary in str(previous_reads[0]["content"])
                assert (seed["agent_root"] / "workspace/next-report.md").read_text() == next_content
                next_results = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == successor.id)))
                assert next_results and all(row.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for row in next_results)
                originals = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id)
                    .execution_options(populate_existing=True)))
                assert any(private_canary in str(row.seal_json["response"].get("content") or "") for row in originals)
            if mutation == "shared_context":
                from app.services.shared_group_authority import read_shared_group_history
                from app.services.session_semantic_history import load_session_semantic_history
                async with app_user_sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    history = await load_session_semantic_history(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], session_id=shared_id)
                    async def read_current_sources():
                        return await read_shared_group_history(db, session=await db.get(ChatSession, shared_id),
                            messages=history.messages, user=await db.get(User, seed["user_id"]))
                    visible, _ = await read_current_sources()
                    assert private_canary in json.dumps([message.content for message in visible])
                    (await db.get(ResourcePermission, note_grant_id)).actions = []
                    await db.commit()
                    visible, holds = await read_current_sources()
                    assert private_canary not in json.dumps([message.content for message in visible])
                    assert any(item["kind"] == "source_access_denied" for item in holds)
                    (await db.get(ResourcePermission, note_grant_id)).actions = ["read"]
                    await db.commit()
                    visible, _ = await read_current_sources()
                    assert private_canary in json.dumps([message.content for message in visible])
