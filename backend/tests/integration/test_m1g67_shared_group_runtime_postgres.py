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


@pytest.mark.parametrize("mutation", [None, "forged_actor", "unlinked", "private_source", "private_recipient", "unknown_recipient", "queued_member", "same_sender", "explicit_other", "private_context", "shared_context", "web_member", "web_socket", "web_browser", "web_question", "web_question_browser", "web_cancel", "web_cancel_recovery", "web_cancel_unknown", "web_cancel_browser", "web_permission", "web_permission_session", "web_permission_deny", "web_permission_browser"])
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

    permission_decision = {"web_permission": "allow_once", "web_permission_session": "allow_session", "web_permission_deny": "deny", "web_permission_browser": "allow_once"}.get(mutation)
    permission_browser = mutation == "web_permission_browser"
    web_permission = permission_decision is not None
    web_requester = mutation == "web_member"
    question_browser = mutation == "web_question_browser"
    web_question = mutation in {"web_question", "web_question_browser"}
    cancel_recovery = mutation == "web_cancel_recovery"
    cancel_unknown = mutation == "web_cancel_unknown"
    cancel_browser = mutation == "web_cancel_browser"
    web_cancel = mutation in {"web_cancel", "web_cancel_recovery", "web_cancel_unknown", "web_cancel_browser"}
    if web_question or web_cancel or web_permission:
        mutation = None
    web_browser = mutation == "web_browser"
    web_socket = mutation in {"web_socket", "web_browser"}
    if web_requester or web_socket:
        mutation = "private_context"
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
        agent.default_session_permission_mode = "default" if web_permission else "bypassPermissions"
        await AgentManager().initialize_agent_files(db, agent, personality="SHARED_EMPLOYEE_ROLE",
            original_author_id=seed["user_id"])
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-group-provider-only")
        model.max_input_tokens = 256_000
        if not web_permission:
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
        if web_question or web_cancel or web_permission or mutation in {"private_recipient", "queued_member", "explicit_other", "private_context", "shared_context"}:
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
        if web_question or web_cancel or web_permission or mutation in {"private_recipient", "unknown_recipient", "queued_member", "explicit_other", "private_context", "shared_context"}:
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
    ingress_claims = {}
    async def submit(sender, subject, content, key, **intent):
        body = {"event": {"sender": {"sender_id": {"open_id": subject}}, "message": {
            "chat_type": "group", "chat_id": "oc_native_group", "message_type": "text", "content": json.dumps({"text": content})}}}
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            event = await accept_authenticated_channel_event(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                provider="feishu", installation_ref=installation, provider_event_id=key,
                handler_key="feishu.event", body=body)
        claimed = await ChannelIngressInboxService(session_factory=app_user_sessionmaker).claim_batch(worker_id="shared-ingress")
        assert any(item.id == event.event_id for item in claimed)
        ingress_claims[key] = next(item for item in claimed if item.id == event.event_id)
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

    async def submit_web(content, key):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from app.api import chat_sessions
        from app.core.security import get_current_user

        async def current_user():
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                return await db.get(User, seed["user_id"])

        async def current_db():
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                yield db

        web = FastAPI()
        web.include_router(chat_sessions.router)
        web.dependency_overrides[get_current_user] = current_user
        web.dependency_overrides[database.get_db] = current_db
        input_id = uuid5(shared_id, key)
        url = f"/agents/{seed['agent_id']}/sessions"
        async with AsyncClient(transport=ASGITransport(app=web), base_url="http://group.fixture") as browser:
            response = await browser.post(f"{url}/{shared_id}/runs", json={
                "content": content, "input_id": str(input_id), "idempotency_key": key})
            (tmp_path / "web-input.json").write_text(response.text)
            assert response.status_code == 202, response.text
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                current_run = await db.get(RuntimeTask, run_id)
                if current_run.status == "pending":
                    current_run.result_summary = private_canary
                    await db.commit()
            activity = await browser.get(f"{url}/{shared_id}/runs/active")
            assert activity.status_code == 200 and "result_summary" not in activity.text and private_canary not in activity.text
            (tmp_path / "web-active-run.json").write_text(activity.text)
            discovery = await browser.get(url, params={"scope": "groups"})
            assert discovery.status_code == 200, discovery.text
            group = next(row for row in discovery.json() if row["id"] == str(shared_id))
            assert group["user_id"] is None and group["audience_kind"] == "group"
            assert not group["is_current_user_session"] and not group["read_only"]
            assert group["active_projection"] is None and private_canary not in discovery.text
            assert not group["permission_profile"] and not group["writable_roots"]
            assert all(row["id"] != str(seed["session_id"]) for row in discovery.json())
            (tmp_path / "web-groups.json").write_text(discovery.text)
            async def no_members(*_args, **_kwargs):
                return set()
            async def members_offline(*_args, **_kwargs):
                raise RuntimeError("Synthetic membership outage")
            for lookup, status in ((no_members, 403), (members_offline, 503)):
                with monkeypatch.context() as patch:
                    patch.setattr(FeishuService, "get_chat_member_open_ids", lookup)
                    denied = await browser.post(f"{url}/{shared_id}/runs", json={
                        "content": content, "input_id": str(input_id), "idempotency_key": key})
                    assert denied.status_code == status, denied.text
                    activity = await browser.get(f"{url}/{shared_id}/runs/active")
                    assert activity.status_code == status, activity.text
                    inventory = await browser.get(url, params={"scope": "groups"})
                    if status == 403:
                        assert inventory.status_code == 200 and inventory.json() == []
                    else:
                        assert inventory.status_code == 503 and inventory.json()["detail"]["code"] == "group_membership_unavailable"
            for endpoint in ("branches", "inputs"):
                if endpoint == "branches":
                    denied = await browser.post(f"{url}/{shared_id}/branches", json={
                        "mode": "branch", "anchor_event_id": str(input_id)})
                else:
                    denied = await browser.post(f"{url}/{shared_id}/inputs", json={
                        "kind": "queue_next_turn", "input_id": str(uuid4()), "idempotency_key": "unsupported-direct-control",
                        "content_parts": [{"type": "text", "text": content}]})
                assert denied.status_code == 403, denied.text
        return SimpleNamespace(event_id=input_id), response.json()

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
            # This helper owns the claim; acknowledge the actual rejection so
            # lease recovery cannot redispatch it into an unrelated test.
            assert await ChannelIngressInboxService(session_factory=app_user_sessionmaker)._mark_processed(
                item=ingress_claims["native-shared-second"], worker_id="shared-ingress",
                receipt={"status": "rejected", "http_status": 409, "reason_code": conflict.value.detail["code"],
                    "external_principal_id": str(sender.external_principal_id)})
        else:
            if web_requester:
                second_event, second = await submit_web(other_content, "native-shared-second")
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
                assert (await db.get(SessionCommand, second_command_id)).principal_id == (seed["user_id"] if web_requester else sender.external_principal_id)
                if mutation in {"queued_member", "private_context", "shared_context"}:
                    from app.services.session_semantic_history import load_session_semantic_history
                    prior = await load_session_semantic_history(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], session_id=shared_id)
                    assert "OTHER_REQUESTER_ONLY" not in json.dumps([message.content for message in prior.messages])
            else:
                assert await db.scalar(select(SessionCommand.id).where(SessionCommand.session_id == shared_id,
                    SessionCommand.idempotency_key == "native-shared-second")) is None
                rejected = await db.get(ChannelIngressEvent, ingress_claims["native-shared-second"].id)
                assert rejected.status == "processed" and rejected.locked_at is None
                assert rejected.processing_receipt_json["status"] == "rejected"
            task.status, task.claimed_by, task.claim_version, task.attempt_count = "running", "shared-worker", 1, 1
            task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            await db.commit()
    if web_cancel:
        await _exercise_group_cancel(seed, shared_id, run_id, member.id, app_user_sessionmaker, monkeypatch, tmp_path,
            recover_cancel=cancel_recovery, unknown_cancel=cancel_unknown, browser_cancel=cancel_browser)
        return
    responses = [
        {"tool_calls": [{"id": "shared-write", "type": "function", "function": {"name": "write_file",
            "arguments": json.dumps({"path": "workspace/shared-report.md", "content": report_content})}}], "finish_reason": "tool_calls"},
        {"tool_calls": [{"id": "shared-read", "type": "function", "function": {"name": "read_file",
            "arguments": json.dumps({"path": "workspace/shared-report.md"})}}], "finish_reason": "tool_calls"},
        {"content": "The shared report is ready.", "finish_reason": "stop"},
    ]
    if web_question:
        responses[-1] = {"tool_calls": [{"id": "group-question", "type": "function", "function": {
            "name": "ask_user_question", "arguments": json.dumps({"questions": [{"question": "Which audience?",
                "header": "Audience", "options": [{"label": "Team"}, {"label": "Personal"}]}]})}}],
            "finish_reason": "tool_calls"}
    if mutation in {"private_context", "shared_context"}:
        responses.insert(0, {"tool_calls": [{"id": "private-read", "type": "function", "function": {"name": "read_file",
            "arguments": json.dumps({"path": "workspace/private-notes.md"})}}], "finish_reason": "tool_calls"})
        responses[-1]["content"] = f"The private report says {private_canary}."
    if mutation is None:
        responses[-1]["content"] = "The [shared report](workspace/shared-report.md) is ready."
    client = ScriptedLLMClient(responses)
    if web_socket:
        import asyncio
        import app.services.web_chat_runtime as runtime
        from app.services.shared_group_history import SharedGroupReaderSocket
        from app.services.web_chat_broker import WebChatBroker
        from app.services.session_model_inputs import _model_dependencies

        class RuntimeReader:
            def __init__(self, label):
                self.label, self.closed = label, []
                self.frames, self.observations = [], []
                self.denied, self.allowed = asyncio.Event(), asyncio.Event()
            async def accept(self):
                pass
            async def close(self, **kwargs):
                self.closed.append(kwargs)
            async def send_json(self, frame):
                self.frames.append(frame)
                if frame.get("result_id") is None:
                    return
                async with app_user_sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    model = await db.get(SessionModelResult, UUID(frame["result_id"]))
                    if model is None:
                        return
                    receipt = frame.get("source_access_receipt") or {}
                    if model.run_id == run_id and receipt.get("status") == "denied":
                        assert private_canary not in json.dumps(frame)
                        self.denied.set()
                    if model.run_id == run_id and private_canary in json.dumps(frame):
                        assert model.state in {"sealed", "round_committed"} and _model_dependencies(model)[1]
                        assert receipt.get("status") == "allowed"
                        self.allowed.set()
                    self.observations.append({"state": model.state, "run_id": str(model.run_id), "frame": frame})

        runtime_reader, source_owner_reader, runtime_broker = RuntimeReader("other"), RuntimeReader("owner"), WebChatBroker()
        monkeypatch.setattr(runtime, "web_chat_broker", runtime_broker)
        await runtime_broker.connect(str(seed["agent_id"]), SharedGroupReaderSocket(runtime_reader,
            tenant_id=seed["tenant_id"], session_id=shared_id, user_id=seed["user_id"]), str(shared_id))
        await runtime_broker.connect(str(seed["agent_id"]), SharedGroupReaderSocket(source_owner_reader,
            tenant_id=seed["tenant_id"], session_id=shared_id, user_id=member.id), str(shared_id))
        client.script[-1]["stream_text"] = private_canary
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    fence = set_runtime_task_fence(task_id=run_id, worker_id="shared-worker", claim_version=1)
    try:
        await execute_web_chat_run(run_id)
    finally:
        reset_runtime_task_fence(fence)
    if web_permission:
        await _exercise_group_permission(seed, shared_id, run_id, member.id, app_user_sessionmaker, monkeypatch, tmp_path,
            decision=permission_decision, model_client=client, submit=submit, actor=actor, other_actor=other.actor, browser_permission=permission_browser)
        return
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
        if web_question:
            await _exercise_group_question(seed, shared_id, run_id, member.id, app_user_sessionmaker, monkeypatch, tmp_path,
                browser_question=question_browser)
            return
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
        if web_requester:
            _, replay = await submit_web(other_content, "native-shared-second")
            assert replay["command_id"] == str(second_command_id)
        else:
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
            assert successor.metadata_json.get("external_principal_id") == (None if web_requester else str(other.actor.external_principal_id))
            assert (await db.get(SessionTurnInput, second_event.event_id)).target_run_id == successor.id
            successor.status, successor.claimed_by = "running", "group-next-requester"
            successor.claim_version, successor.attempt_count = 1, 1
            successor.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            from app.services.shared_group_authority import load_shared_group_runtime_actor
            restored = await load_shared_group_runtime_actor(db, task=successor, session=await db.get(ChatSession, shared_id))
            assert restored.id == seed["user_id"] and getattr(restored, "external_principal_id", None) == (None if web_requester else other.actor.external_principal_id)
            if web_requester:
                from copy import deepcopy
                from app.services.shared_group_authority import bind_shared_group_permission_source
                command = await db.get(SessionCommand, second_command_id)
                permissions = deepcopy(command.target_json["runtime_metadata"])
                invocation = SimpleNamespace(session_context=SimpleNamespace(metadata=permissions), allowed_tool_names=())
                await bind_shared_group_permission_source(db, task=successor, session=await db.get(ChatSession, shared_id), invocation=invocation)
                source = permissions["_session_permission_context_source"]
                assert source.reference == {"source_kind": "session_command", "source_id": str(command.id), "source_version": command.request_hash}
                permissions["writable_roots"] = ["workspace/forged"]
                await bind_shared_group_permission_source(db, task=successor, session=await db.get(ChatSession, shared_id), invocation=invocation)
                assert "_session_permission_context_source" not in permissions
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
            if web_socket:
                (tmp_path / "stream-observations.json").write_text(json.dumps({"other": runtime_reader.observations,
                    "owner": source_owner_reader.observations, "owner_closed": source_owner_reader.closed}, default=str, indent=2))
                assert runtime_reader.denied.is_set() and source_owner_reader.allowed.is_set()
                assert private_canary not in json.dumps(runtime_reader.frames)
                (tmp_path / "runtime-socket-frames.json").write_text(json.dumps({"other_reader": runtime_reader.observations, "source_owner": source_owner_reader.observations}, default=str, indent=2))
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
            from fastapi import FastAPI
            from httpx import ASGITransport, AsyncClient
            from app.api import chat_sessions
            from app.core.security import get_current_user

            async def reader_user():
                async with app_user_sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    return await db.get(User, seed["user_id"])

            async def reader_db():
                async with app_user_sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    yield db

            web = FastAPI()
            web.include_router(chat_sessions.router)
            web.dependency_overrides[get_current_user] = reader_user
            web.dependency_overrides[database.get_db] = reader_db
            async with AsyncClient(transport=ASGITransport(app=web), base_url="http://group.fixture") as browser:
                url = f"/agents/{seed['agent_id']}/sessions/{shared_id}"
                response = await browser.get(url + "/transcript", params={"schema_version": 2, "limit": 1000})
                (tmp_path / "browser-transcript.json").write_text(response.text)
                assert response.status_code == 200, response.text
                content = json.dumps(response.json())
                assert "The independent report is ready." in content
                assert (private_canary in content) == (mutation == "shared_context")
                stored = await browser.get(url + "/messages")
                (tmp_path / "browser-messages.json").write_text(stored.text)
                assert stored.status_code == 200, stored.text
                assert (private_canary in stored.text) == (mutation == "shared_context")
                if mutation == "private_context":
                    pages, after = [], 0
                    while True:
                        page = await browser.get(url + "/transcript", params={"schema_version": 2, "limit": 31, "after_sequence": after})
                        assert page.status_code == 200, page.text
                        rows = page.json()
                        if not rows:
                            break
                        assert rows[0]["sequence"] > after
                        pages.extend(rows)
                        after = rows[-1]["sequence"]
                    assert [row["event_id"] for row in pages] == [row["event_id"] for row in response.json()]
                    assert private_canary not in json.dumps(pages)
                    newest = await browser.get(url + "/transcript", params={"schema_version": 2, "limit": 31, "direction": "backward"})
                    assert newest.status_code == 200
                    assert [row["event_id"] for row in newest.json()] == [row["event_id"] for row in response.json()][-31:]
                    assert any(row["source_access_receipt"]["status"] == "denied" for row in pages)
                else:
                    async with app_user_sessionmaker() as db:
                        await pin_rls_tenant_context(db, seed["tenant_id"])
                        (await db.get(ResourcePermission, note_grant_id)).actions = []
                        await db.commit()
                    for endpoint, params in (("/transcript", {"schema_version": 2, "limit": 1000}), ("/messages", {})):
                        revoked = await browser.get(url + endpoint, params=params)
                        (tmp_path / ("browser-revoked" + endpoint.replace("/", "-") + ".json")).write_text(revoked.text)
                        assert revoked.status_code == 200 and private_canary not in revoked.text, revoked.text
                        assert "OTHER_REQUESTER_ONLY" in revoked.text
                    async with app_user_sessionmaker() as db:
                        await pin_rls_tenant_context(db, seed["tenant_id"])
                        (await db.get(ResourcePermission, note_grant_id)).actions = ["read"]
                        await db.commit()
                    restored = await browser.get(url + "/transcript", params={"schema_version": 2, "limit": 1000})
                    (tmp_path / "browser-restored.json").write_text(restored.text)
                    assert restored.status_code == 200 and private_canary in restored.text

                if mutation == "private_context":
                    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

                    async with app_user_sessionmaker() as db:
                        await pin_rls_tenant_context(db, seed["tenant_id"])
                        public_model = await db.get(SessionModelResult, next_results[0].id)
                        await _validate_input_dependencies(db, seed["tenant_id"],
                            public_model.model_request_snapshot_json["input_bindings"]["dependencies"],
                            user=await db.get(User, seed["user_id"]))
                        committed = await db.get(ChatTranscriptEvent, public_model.round_committed_event_id)
                        await append_session_events(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], session_id=shared_id,
                            drafts=[SessionEventDraft(item_id=uuid4(), item_kind="hook", lifecycle="completed",
                                scope=committed.scope_json, actor={"type": "runtime"}, result_id=public_model.id,
                                payload={"boundary": "PreToolUse", "stdout": (seed["agent_root"] / "workspace/private-notes.md").read_text()})])
                        await db.commit()
                    hook = await browser.get(url + "/transcript", params={"schema_version": 2, "limit": 1000})
                    (tmp_path / "browser-unbound-hook.json").write_text(hook.text)
                    assert hook.status_code == 200 and private_canary not in hook.text, hook.text

                if web_socket:
                    import asyncio
                    from fastapi import WebSocketDisconnect
                    from app.api import websocket as socket_api
                    from app.core.security import create_access_token
                    from app.services.web_chat_broker import WebChatBroker
                    from app.services.session_event_contract import serialize_session_event

                    class Socket:
                        def __init__(self):
                            self.sent, self.closed = [], []
                            self.ready_callback = None
                            self.live, self.incoming = asyncio.Event(), asyncio.Queue()
                            self.subscribed = False
                        async def accept(self):
                            pass
                        async def send_json(self, frame):
                            self.sent.append(frame)
                            if frame.get("type") == "session.ready" and self.ready_callback:
                                await self.ready_callback()
                        async def close(self, code=1000, reason=""):
                            self.closed.append({"code": code, "reason": reason})
                            self.live.set()
                        async def receive_json(self):
                            if not self.subscribed:
                                self.subscribed = True
                                return {"type": "session.subscribe", "session_id": str(shared_id),
                                    "schema_version": 2, "after_sequence": 0, "connection_attempt_id": "native-group-reader"}
                            self.live.set()
                            await self.incoming.get()
                            raise WebSocketDisconnect()

                    socket, broker = Socket(), runtime_broker
                    monkeypatch.setattr(socket_api, "manager", broker)
                    async def during_ready():
                        await broker.send_session_message(str(seed["agent_id"]), str(shared_id),
                            {"type": "chunk", "content": private_canary, "run_id": str(run_id)})
                    socket.ready_callback = during_ready
                    token = create_access_token(str(seed["user_id"]), "user", str(seed["tenant_id"]))
                    transport = asyncio.create_task(socket_api.websocket_chat(socket, seed["agent_id"], token, str(shared_id)))
                    try:
                        await asyncio.wait_for(socket.live.wait(), 10)
                        assert any(frame.get("type") == "session.ready" for frame in socket.sent), socket.sent
                        assert not socket.closed, socket.closed
                        assert private_canary not in json.dumps(socket.sent)
                        assert "The independent report is ready." in json.dumps(socket.sent)
                        async with app_user_sessionmaker() as db:
                            await pin_rls_tenant_context(db, seed["tenant_id"])
                            event = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == shared_id,
                                ChatTranscriptEvent.item_kind == "hook").order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
                            envelope = serialize_session_event(event, audience="user")
                        await broker.send_session_message(str(seed["agent_id"]), str(shared_id), envelope)
                        await broker.send_session_message(str(seed["agent_id"]), str(shared_id),
                            {"type": "chunk", "content": private_canary, "run_id": str(run_id)})
                        await broker.send_session_message(str(seed["agent_id"]), str(shared_id),
                            {"type": "session.ready", "content": private_canary})
                        await broker.send_session_message(str(seed["agent_id"]), str(shared_id),
                            {"type": "session.control_receipt", "receipt": {"run": {"result_summary": private_canary}}})
                        assert private_canary not in json.dumps(socket.sent)
                        assert any(frame.get("source_access_receipt") or (frame.get("payload") or {}).get("source_access_receipt")
                            for frame in socket.sent)
                        (tmp_path / "browser-socket.json").write_text(json.dumps(socket.sent, default=str, indent=2))
                        async def removed_member(*_args, **_kwargs):
                            return {"ou_group_requester"}
                        with monkeypatch.context() as patch:
                            patch.setattr(FeishuService, "get_chat_member_open_ids", removed_member)
                            await broker.send_session_message(str(seed["agent_id"]), str(shared_id), envelope)
                            assert socket.closed[-1]["code"] == 4403
                            assert socket.sent[-1]["error"]["reason_code"] == "group_membership_required"
                        async with app_user_sessionmaker() as db:
                            await pin_rls_tenant_context(db, seed["tenant_id"])
                            assert (await db.get(RuntimeTask, successor.id)).status == "completed"
                            assert (await db.get(ChatSession, shared_id)).delivery_target_json["receive_id"] == "oc_native_group"
                    finally:
                        socket.incoming.put_nowait(None)
                        await asyncio.wait_for(transport, 10)

                if web_browser:
                    import socket as sockets
                    from pathlib import Path
                    import uvicorn
                    from fastapi.responses import FileResponse, JSONResponse
                    from fastapi.staticfiles import StaticFiles
                    from app.api import agents, auth
                    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
                    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

                    processor = WebTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=seed["data_root"])
                    settled = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
                        tenant_id=seed["tenant_id"], worker_id="browser-prior-terminal", canonical_validator=processor.validate,
                        process_callback=processor, task_types=("web_chat_turn",))
                    assert settled["delivered"] == 1, settled
                    warmup, warmup_receipt = await submit(actor, "ou_group_requester", "Wait for the Web addendum.", "browser-prior-work")
                    async with app_user_sessionmaker() as db:
                        await pin_rls_tenant_context(db, seed["tenant_id"])
                        warmup_command = UUID(warmup_receipt["command_id"])
                        await recover_admitted_session_inputs_once(db, worker_id="browser-active-work", tenant_id=seed["tenant_id"],
                            admission_ids=(uuid5(warmup_command, "input-admission:revision:1"),))
                        active = await db.scalar(select(RuntimeTask).where(RuntimeTask.metadata_json["session_v2_input_id"].astext == str(warmup.event_id)))
                        assert active is not None and active.status == "pending" and active.root_user_id == member.id
                    frontend = Path(__file__).resolve().parents[3] / "frontend"
                    assert (frontend / "dist/index.html").is_file(), "Build the isolated frontend before the native browser test."
                    web.include_router(chat_sessions.router, prefix="/api")
                    web.include_router(agents.router, prefix="/api")
                    web.include_router(auth.router, prefix="/api")
                    web.include_router(socket_api.router)
                    monkeypatch.setattr(socket_api, "manager", runtime_broker)
                    web.mount("/assets", StaticFiles(directory=frontend / "dist/assets"), name="assets")
                    @web.get("/api/{path:path}")
                    async def unavailable_fixture_api(path: str):
                        return JSONResponse(status_code=404, content={"detail": "Unsupported fixture endpoint"})
                    @web.get("/{path:path}")
                    async def built_frontend(path: str):
                        return FileResponse(frontend / "dist/index.html")
                    listener = sockets.socket()
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                    server = uvicorn.Server(uvicorn.Config(web, host="127.0.0.1", port=port, lifespan="off", access_log=False, log_level="warning"))
                    server_task = asyncio.create_task(server.serve(sockets=[listener]))
                    process = None
                    try:
                        for _ in range(500):
                            if server.started:
                                break
                            assert not server_task.done()
                            await asyncio.sleep(0.01)
                        assert server.started
                        fixture_path = tmp_path / "browser-fixture.json"
                        fixture_path.write_text(json.dumps({"origin": f"http://127.0.0.1:{port}", "token": token,
                            "tenantId": str(seed["tenant_id"]), "agentId": str(seed["agent_id"]), "sessionId": str(shared_id),
                            "activeRunId": active.id.hex, "privateCanary": private_canary, "privateSessionId": str(seed["session_id"]),
                            "privateScreenshot": str(tmp_path / "browser-private-outage.png"),
                            "entryScreenshot": str(tmp_path / "browser-entry.png"), "deniedScreenshot": str(tmp_path / "browser-denied.png"),
                            "receiptPath": str(tmp_path / "browser-input-receipt.json"), "socketPath": str(tmp_path / "browser-socket-frames.json"),
                            "errorPath": str(tmp_path / "browser-error.json"), "errorScreenshot": str(tmp_path / "browser-error.png")}))
                        process = await asyncio.create_subprocess_exec("node", str(frontend / "e2e/group-work-native.mjs"), str(fixture_path),
                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        line = await asyncio.wait_for(process.stdout.readline(), 60)
                        (tmp_path / "browser-driver-first.log").write_bytes(line)
                        assert line and json.loads(line)["event"] == "input_recorded", (line, await process.stderr.read())
                        browser_input = json.loads(line)
                        async def member_removed(*_args, **_kwargs):
                            return {"ou_group_requester"}
                        with monkeypatch.context() as patch:
                            patch.setattr(FeishuService, "get_chat_member_open_ids", member_removed)
                            await runtime_broker.send_session_message(str(seed["agent_id"]), str(shared_id), envelope)
                            revoked = await asyncio.wait_for(process.stdout.readline(), 30)
                            assert revoked and json.loads(revoked)["event"] == "read_revoked", revoked
                            async def members_unavailable(*_args, **_kwargs):
                                raise RuntimeError("Synthetic membership outage")
                            patch.setattr(FeishuService, "get_chat_member_open_ids", members_unavailable)
                            process.stdin.write(b'{"event":"provider_offline"}\n')
                            await process.stdin.drain()
                            stdout, stderr = await asyncio.wait_for(process.communicate(), 45)
                            (tmp_path / "browser-driver.log").write_bytes(line + revoked + stdout + stderr)
                            assert process.returncode == 0, (stdout, stderr)
                        async with app_user_sessionmaker() as db:
                            await pin_rls_tenant_context(db, seed["tenant_id"])
                            command = await db.get(SessionCommand, UUID(browser_input["commandId"]))
                            assert command.principal_type == "user" and command.principal_id == seed["user_id"]
                            assert (await db.get(SessionTurnInput, UUID(browser_input["inputId"]))).intent == "queue_next_turn"
                            assert (await db.get(RuntimeTask, active.id)).status == "pending"
                            assert (await db.get(ChatSession, shared_id)).delivery_target_json["receive_id"] == "oc_native_group"
                    finally:
                        if process is not None and process.returncode is None:
                            process.terminate()
                            await process.communicate()
                        server.should_exit = True
                        await asyncio.wait_for(server_task, 10)
                        listener.close()

                async def without_reader(*_args, **_kwargs):
                    return {"ou_group_requester"}
                monkeypatch.setattr(FeishuService, "get_chat_member_open_ids", without_reader)
                assert (await browser.get(url + "/transcript", params={"schema_version": 2})).status_code == 403
                assert (await browser.get(url + "/messages")).status_code == 403
                async def membership_offline(*_args, **_kwargs):
                    raise RuntimeError("Synthetic membership service unavailable")
                monkeypatch.setattr(FeishuService, "get_chat_member_open_ids", membership_offline)
                offline = await browser.get(url + "/transcript", params={"schema_version": 2})
                assert offline.status_code == 503 and offline.json()["detail"]["code"] == "group_membership_unavailable"

    if web_requester:
        from app.models.security_audit import SecurityAuditEvent
        monkeypatch.setattr(FeishuService, "get_chat_member_open_ids", members)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            (await db.get(User, seed["user_id"])).role = "org_admin"
            await db.commit()
        async with AsyncClient(transport=ASGITransport(app=web), base_url="http://group.fixture") as browser:
            admin_input = uuid5(shared_id, "web-admin-addendum")
            admin_response = await browser.post(url + "/runs", json={"content": "Administrator addendum.",
                "input_id": str(admin_input), "idempotency_key": "web-admin-addendum"})
            assert admin_response.status_code in {201, 202}, admin_response.text
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            audits = list(await db.scalars(select(SecurityAuditEvent).where(
                SecurityAuditEvent.tenant_id == seed["tenant_id"], SecurityAuditEvent.resource_id == shared_id,
                SecurityAuditEvent.event_type == "session.scoped_business_admin_access",
                SecurityAuditEvent.action == "mutate_session_input")))
            assert len(audits) == 1 and audits[0].actor_id == seed["user_id"]
            command = await db.scalar(select(SessionCommand).where(SessionCommand.session_id == shared_id,
                SessionCommand.idempotency_key == "web-admin-addendum"))
            assert command.principal_type == "user" and command.principal_id == seed["user_id"]
            assert audits[0].details["authority_source"] == "scoped_business_admin"
            assert (await db.get(ChatSession, shared_id)).user_id is None


async def _exercise_group_question(seed, session_id, run_id, user_id, sessionmaker, monkeypatch, tmp_path, *, browser_question=False):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app import database
    from app.api import chat_sessions
    from app.core.security import get_current_user
    from app.database import pin_rls_tenant_context
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.chat_session import ChatSession
    from app.models.session_v2 import SessionCommand, SessionTurnInput
    from app.models.runtime_task import RuntimeTask
    from app.models.security_audit import ResourcePermission
    from app.core.resource_authority import workspace_resource_id
    from app.services.feishu_service import FeishuService
    from app.services.session_input_dispatch import recover_admitted_session_inputs_once
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run
    import app.runtime.invoker as invoker

    reader_id = user_id
    from app.models.user import User

    async def current_user():
        async with sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            return await db.get(User, reader_id)

    async def scoped_db():
        async with sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db

    async with sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        questions = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.run_id == run_id, ChatTranscriptEvent.item_kind == "user_question")))
        assert len(questions) == 1 and questions[0].lifecycle == "waiting"
        question = questions[0]
        assert question.invocation_id is not None
    web = FastAPI()
    web.include_router(chat_sessions.router)
    web.dependency_overrides[get_current_user] = current_user
    web.dependency_overrides[database.get_db] = scoped_db
    input_id = uuid4()
    url = f"/agents/{seed['agent_id']}/sessions/{session_id}"
    body = {"kind": "answer_request", "input_id": str(input_id), "idempotency_key": f"question-answer:{input_id}",
        "request_item_id": str(question.item_id), "content_parts": [{"type": "text", "text": "Audience: Team"}],
        "terminal_fallback": "queue_next_turn"}
    server = server_task = listener = process = None
    try:
        async with AsyncClient(transport=ASGITransport(app=web), base_url="http://group.fixture") as client:
            transcript = await client.get(url + "/transcript", params={"schema_version": 2, "limit": 1000})
            assert transcript.status_code == 200 and "Which audience?" in transcript.text, transcript.text
            reader_id = seed["user_id"]
            restricted = await client.get(url + "/transcript", params={"schema_version": 2, "limit": 1000})
            assert restricted.status_code == 200 and "Which audience?" not in restricted.text, restricted.text
            denied = await client.post(url + "/inputs", json=body)
            assert denied.status_code == 403 and denied.json()["detail"]["source_access_receipt"]["status"] == "denied", denied.text
            async with sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                db.add(ResourcePermission(id=uuid4(), tenant_id=seed["tenant_id"], principal_type="user",
                    principal_id=seed["user_id"], resource_type="workspace_file",
                    resource_id=workspace_resource_id(seed["agent_id"], "workspace/shared-report.md"),
                    actions=["read"], effect="allow", conditions={}))
                await db.commit()
            shared_question = await client.get(url + "/transcript", params={"schema_version": 2, "limit": 1000})
            assert shared_question.status_code == 200 and "Which audience?" in shared_question.text, shared_question.text
            other_reader = await client.post(url + "/inputs", json=body)
            assert other_reader.status_code == 409 and other_reader.json()["detail"]["code"] == "shared_group_requester_conflict", other_reader.text
            reader_id = user_id
            for status, members in [(403, set()), (503, None)]:
                async def unavailable_members(*_args, **_kwargs):
                    if members is None:
                        raise RuntimeError("Synthetic membership unavailable")
                    return members
                with monkeypatch.context() as patch:
                    patch.setattr(FeishuService, "get_chat_member_open_ids", unavailable_members)
                    unavailable = await client.post(url + "/inputs", json=body)
                    assert unavailable.status_code == status, unavailable.text
            async with sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                assert await db.get(SessionTurnInput, input_id) is None
            if browser_question:
                import asyncio
                import socket as sockets
                from pathlib import Path
                import uvicorn
                from fastapi.responses import FileResponse, JSONResponse
                from fastapi.staticfiles import StaticFiles
                from app.api import agents, auth, websocket as socket_api
                from app.core.security import create_access_token
                from app.services.web_chat_broker import WebChatBroker
                import app.services.web_chat_runtime as runtime

                frontend = Path(__file__).resolve().parents[3] / "frontend"
                assert (frontend / "dist/index.html").is_file()
                broker = WebChatBroker()
                monkeypatch.setattr(runtime, "web_chat_broker", broker)
                monkeypatch.setattr(socket_api, "manager", broker)
                web.include_router(chat_sessions.router, prefix="/api")
                web.include_router(agents.router, prefix="/api")
                web.include_router(auth.router, prefix="/api")
                web.include_router(socket_api.router)
                web.mount("/assets", StaticFiles(directory=frontend / "dist/assets"), name="assets")
                @web.get("/api/{path:path}")
                async def unavailable_fixture_api(path: str):
                    return JSONResponse(status_code=404, content={"detail": "Unsupported fixture endpoint"})
                @web.get("/{path:path}")
                async def built_frontend(path: str):
                    return FileResponse(frontend / "dist/index.html")
                listener = sockets.socket()
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
                server = uvicorn.Server(uvicorn.Config(web, host="127.0.0.1", port=port, lifespan="off", access_log=False, log_level="warning"))
                server_task = asyncio.create_task(server.serve(sockets=[listener]))
                for _ in range(500):
                    if server.started:
                        break
                    assert not server_task.done()
                    await asyncio.sleep(0.01)
                assert server.started
                fixture_path = tmp_path / "question-browser-fixture.json"
                fixture_path.write_text(json.dumps({"origin": f"http://127.0.0.1:{port}", "tenantId": str(seed["tenant_id"]),
                    "agentId": str(seed["agent_id"]), "sessionId": str(session_id), "questionId": str(question.item_id),
                    "readerToken": create_access_token(str(seed["user_id"]), "user", str(seed["tenant_id"])),
                    "requesterToken": create_access_token(str(user_id), "user", str(seed["tenant_id"])),
                    **{key: str(tmp_path / name) for key, name in {"readonlyScreenshot": "question-readonly.png",
                        "completedScreenshot": "question-completed.png", "answerPath": "question-browser-answer.json",
                        "framesPath": "question-browser-frames.json", "errorPath": "question-browser-error.json",
                        "errorScreenshot": "question-browser-error.png"}.items()}}))
                reader_id = seed["user_id"]
                process = await asyncio.create_subprocess_exec("node", str(frontend / "e2e/group-question-native.mjs"), str(fixture_path),
                    cwd=frontend, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                line = await asyncio.wait_for(process.stdout.readline(), 60)
                assert line and json.loads(line)["event"] == "readonly_checked", (line, await process.stderr.read())
                reader_id = user_id
                process.stdin.write(b'{"event":"requester_ready"}\n')
                await process.stdin.drain()
                line = await asyncio.wait_for(process.stdout.readline(), 60)
                assert line and json.loads(line)["event"] == "answer_recorded", (line, await process.stderr.read())
                recorded = json.loads((tmp_path / "question-browser-answer.json").read_text())
                body, receipt = recorded["body"], recorded["receipt"]
                input_id = UUID(body["input_id"])
                assert body["content_parts"] == [{"type": "text", "text": "Audience: Team"}]
            else:
                response = await client.post(url + "/inputs", json=body)
                receipt = response.json()
                assert response.status_code == 200, response.text
            (tmp_path / "group-answer-response.json").write_text(json.dumps(receipt))
            assert receipt["admission_state"] == "admitted", receipt
            replay = await client.post(url + "/inputs", json=body)
            assert replay.status_code == 200 and replay.json()["command_id"] == receipt["command_id"], replay.text
            command_id = UUID(receipt["command_id"])
        from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
        from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

        processor = WebTerminalBoundaryProcessor(session_factory=sessionmaker, data_root=seed["data_root"])
        terminal = await RuntimeTerminalBoundaryOutboxService(session_factory=sessionmaker).drain_once(
            tenant_id=seed["tenant_id"], worker_id="group-question-terminal", canonical_validator=processor.validate,
            process_callback=processor, task_types=("web_chat_turn",))
        assert terminal["delivered"] == 1, terminal
        async with sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            answer = await db.get(SessionTurnInput, input_id)
            assert answer is not None and answer.request_item_id == question.item_id and answer.session_id == session_id
            command = await db.get(SessionCommand, command_id)
            assert command.principal_type == "user" and command.principal_id == user_id
            assert command.target_json["expected_run_id"] == str(run_id)
            await recover_admitted_session_inputs_once(db, worker_id="group-answer-dispatch", tenant_id=seed["tenant_id"],
                admission_ids=(uuid5(command_id, "input-admission:revision:1"),))
            successor = await db.get(RuntimeTask, uuid5(input_id, "session-v2-successor-run"))
            assert successor is not None and successor.root_user_id == user_id and successor.parent_session_id == str(session_id)
            completed_question = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.item_id == question.item_id,
                ChatTranscriptEvent.input_id == input_id, ChatTranscriptEvent.lifecycle == "completed"))
            assert completed_question is not None and completed_question.run_id == run_id
            successor.status, successor.claimed_by, successor.claim_version, successor.attempt_count = "running", "group-answer-worker", 1, 1
            successor.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            await db.commit()
        model = ScriptedLLMClient([
            {"tool_calls": [{"id": "answer-read", "type": "function", "function": {"name": "read_file",
                "arguments": json.dumps({"path": "workspace/shared-report.md"})}}], "finish_reason": "tool_calls"},
            {"content": "The Team report is ready.", "finish_reason": "stop"},
        ])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: model)
        fence = set_runtime_task_fence(task_id=successor.id, worker_id="group-answer-worker", claim_version=1)
        try:
            await execute_web_chat_run(successor.id)
        finally:
            reset_runtime_task_fence(fence)
        (tmp_path / "group-answer-model-requests.json").write_text(json.dumps(model.requests, default=str, indent=2))
        async with sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            settled = await db.get(RuntimeTask, successor.id)
            assert settled.status == "completed", (settled.status, settled.result_summary, settled.metadata_json)
            assert len(model.requests) == 2 and "Audience: Team" in json.dumps(model.requests[0]["messages"])
            assert "GROUP_REPORT" in json.dumps(model.requests[1]["messages"])
            assert settled.metadata_json["channel"] == "web"
            work = await db.get(ChatSession, session_id)
            assert work.source_channel == "feishu" and work.delivery_target_json["receive_id"] == "oc_native_group"
        if browser_question:
            line = await asyncio.wait_for(process.stdout.readline(), 60)
            assert line and json.loads(line)["event"] == "work_completed", (line, await process.stderr.read())
            assert await asyncio.wait_for(process.wait(), 10) == 0, await process.stderr.read()
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 10)
        if server is not None:
            server.should_exit = True
        try:
            if server_task is not None:
                await asyncio.wait_for(server_task, 10)
        finally:
            if listener is not None:
                listener.close()


async def _exercise_group_cancel(seed, session_id, run_id, requester_id, sessionmaker, monkeypatch, tmp_path, *, recover_cancel=False, unknown_cancel=False, browser_cancel=False):
    import asyncio
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app import database
    from app.api import chat_sessions
    from app.core.security import get_current_user
    from app.database import pin_rls_tenant_context
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionControlInput
    from app.models.user import User
    from app.services.feishu_service import FeishuService
    from app.services.session_control_input import recover_stale_cancel_control_inputs_once
    import app.services.session_control_input as controls
    import app.services.web_chat_runtime as runtime
    import app.runtime.invoker as invoker

    reader_id = requester_id
    async def current_user():
        async with sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            return await db.get(User, reader_id)

    async def scoped_db():
        async with sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db

    web = FastAPI()
    web.include_router(chat_sessions.router)
    web.dependency_overrides[get_current_user] = current_user
    web.dependency_overrides[database.get_db] = scoped_db
    url = f"/agents/{seed['agent_id']}/sessions/{session_id}"
    entered, release = asyncio.Event(), asyncio.Event()
    class HoldingClient(ScriptedLLMClient):
        async def stream(self, **kwargs):
            entered.set()
            await release.wait()
            return await super().stream(**kwargs)
    client_class = HoldingClient if unknown_cancel else ScriptedLLMClient
    if not unknown_cancel:
        import app.kernel.engine as engine
        actual_tool_call = engine._execute_tool_call_with_cancel
        async def held_before_tool(*args, **kwargs):
            entered.set()
            await release.wait()
            return await actual_tool_call(*args, **kwargs)
        monkeypatch.setattr(engine, "_execute_tool_call_with_cancel", held_before_tool)
    model = client_class([{"tool_calls": [{"id": "after-stop", "type": "function", "function": {"name": "write_file",
        "arguments": json.dumps({"path": "workspace/after-stop.md", "content": "MUST_NOT_EXECUTE"})}}], "finish_reason": "tool_calls"}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: model)
    signals = []
    actual_signal = runtime.signal_web_chat_cancel
    async def signal(**kwargs):
        signals.append(kwargs)
        return await actual_signal(**kwargs)
    monkeypatch.setattr(runtime, "signal_web_chat_cancel", signal)
    worker = server = server_task = listener = process = None
    if browser_cancel:
        from app.api import websocket as socket_api
        from app.services.web_chat_broker import WebChatBroker
        broker = WebChatBroker()
        monkeypatch.setattr(runtime, "web_chat_broker", broker)
        monkeypatch.setattr(socket_api, "manager", broker)
    try:
        async with AsyncClient(transport=ASGITransport(app=web), base_url="http://group.fixture") as client:
            original = await client.get(url + "/runs/active")
            assert original.status_code == 200 and original.json()["can_cancel"] is True, original.text
            reader_id = seed["user_id"]
            other = await client.get(url + "/runs/active")
            assert other.status_code == 200 and other.json()["can_cancel"] is False, other.text
            denied = await client.post(url + f"/runs/{run_id}/cancel")
            assert denied.status_code == 409 and denied.json()["detail"]["code"] == "shared_group_requester_conflict", denied.text
            reader_id = requester_id
            wrong = await client.post(url + f"/runs/{seed['run_id']}/cancel")
            assert wrong.status_code == 409, wrong.text
            for expected, result in [(403, set()), (503, None)]:
                async def unavailable_members(*_args, **_kwargs):
                    if result is None:
                        raise RuntimeError("Synthetic membership unavailable")
                    return result
                with monkeypatch.context() as patch:
                    patch.setattr(FeishuService, "get_chat_member_open_ids", unavailable_members)
                    response = await client.post(url + f"/runs/{run_id}/cancel")
                    assert response.status_code == expected, response.text
            async with sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                assert list(await db.scalars(select(SessionControlInput).where(SessionControlInput.session_id == session_id))) == []
            assert runtime.dispatch_web_chat_run(run_id, claim_version=1, worker_id="shared-worker")
            worker = runtime._TASKS[run_id.hex]
            await asyncio.wait_for(entered.wait(), 30)
            if recover_cancel:
                async def crash_before_begin(*_args, **_kwargs):
                    raise RuntimeError("Synthetic ingress crash after acceptance")
                with monkeypatch.context() as patch:
                    patch.setattr(controls, "begin_cancel_control_input", crash_before_begin)
                    with pytest.raises(RuntimeError, match="Synthetic ingress crash"):
                        await client.post(url + f"/runs/{run_id}/cancel")
                assert signals == []
                for expected, result in [("denied", set()), ("unavailable", None)]:
                    async def unavailable_members(*_args, **_kwargs):
                        if result is None:
                            raise RuntimeError("Synthetic membership unavailable")
                        return result
                    with monkeypatch.context() as patch:
                        patch.setattr(FeishuService, "get_chat_member_open_ids", unavailable_members)
                        async with sessionmaker() as db:
                            await pin_rls_tenant_context(db, seed["tenant_id"])
                            recovered = await recover_stale_cancel_control_inputs_once(db, worker_id="group-cancel-recovery",
                                stale_after=timedelta(0), tenant_id=seed["tenant_id"])
                            assert recovered["unavailable"] == 1 and recovered["signalled"] == 0, recovered
                            task = await db.get(RuntimeTask, run_id)
                            assert task.status == "running" and task.metadata_json["cancel_signal_delivery"]["delivery_state"] == expected
                            assert task.metadata_json["cancel_signal_delivery"]["recovery_action"] == "restore_group_access"
                    assert signals == [] and not worker.done()
                async with sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    recovered = await recover_stale_cancel_control_inputs_once(db, worker_id="group-cancel-recovery",
                        stale_after=timedelta(0), tenant_id=seed["tenant_id"])
                    assert recovered["signalled"] == 0 and recovered["local_delivered"] == 1 and recovered["unavailable"] == 1, recovered
            else:
                if browser_cancel:
                    import socket as sockets
                    from pathlib import Path
                    import uvicorn
                    from fastapi.responses import FileResponse, JSONResponse
                    from fastapi.staticfiles import StaticFiles
                    from app.api import agents, auth
                    from app.core.security import create_access_token

                    frontend = Path(__file__).resolve().parents[3] / "frontend"
                    assert (frontend / "dist/index.html").is_file()
                    web.include_router(chat_sessions.router, prefix="/api")
                    web.include_router(agents.router, prefix="/api")
                    web.include_router(auth.router, prefix="/api")
                    web.include_router(socket_api.router)
                    web.mount("/assets", StaticFiles(directory=frontend / "dist/assets"), name="assets")
                    @web.get("/api/{path:path}")
                    async def unavailable_fixture_api(path: str):
                        return JSONResponse(status_code=404, content={"detail": "Unsupported fixture endpoint"})
                    @web.get("/{path:path}")
                    async def built_frontend(path: str):
                        return FileResponse(frontend / "dist/index.html")
                    listener = sockets.socket()
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                    server = uvicorn.Server(uvicorn.Config(web, host="127.0.0.1", port=port, lifespan="off", access_log=False, log_level="warning"))
                    server_task = asyncio.create_task(server.serve(sockets=[listener]))
                    for _ in range(500):
                        if server.started:
                            break
                        assert not server_task.done()
                        await asyncio.sleep(0.01)
                    assert server.started
                    fixture_path = tmp_path / "cancel-browser-fixture.json"
                    fixture_path.write_text(json.dumps({"origin": f"http://127.0.0.1:{port}", "tenantId": str(seed["tenant_id"]),
                        "agentId": str(seed["agent_id"]), "sessionId": str(session_id), "runId": run_id.hex,
                        "readerToken": create_access_token(str(seed["user_id"]), "user", str(seed["tenant_id"])),
                        "requesterToken": create_access_token(str(requester_id), "user", str(seed["tenant_id"])),
                        **{key: str(tmp_path / name) for key, name in {"readonlyScreenshot": "cancel-readonly.png",
                            "unconfirmedScreenshot": "cancel-unconfirmed.png", "completedScreenshot": "cancel-completed.png",
                            "receiptPath": "cancel-browser-receipt.json", "errorPath": "cancel-browser-error.json",
                            "errorScreenshot": "cancel-browser-error.png"}.items()}}))
                    reader_id = seed["user_id"]
                    process = await asyncio.create_subprocess_exec("node", str(frontend / "e2e/group-cancel-native.mjs"), str(fixture_path),
                        cwd=frontend, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    line = await asyncio.wait_for(process.stdout.readline(), 60)
                    assert line and json.loads(line)["event"] == "readonly_checked", line
                    reader_id = requester_id
                    process.stdin.write(json.dumps({"event": "requester_ready"}).encode() + bytes([10]))
                    await process.stdin.drain()
                    line = await asyncio.wait_for(process.stdout.readline(), 60)
                    assert line and json.loads(line)["event"] == "cancel_recorded", line
                    receipt = json.loads((tmp_path / "cancel-browser-receipt.json").read_text())
                else:
                    response = await client.post(url + f"/runs/{run_id}/cancel")
                    receipt = response.json()
                    assert response.status_code == 200, response.text
                (tmp_path / "group-cancel-response.json").write_text(json.dumps(receipt))
                assert receipt["status"] == "applying" and receipt["accepted"] is True, receipt
            if unknown_cancel:
                await asyncio.wait_for(asyncio.shield(worker), 30)
            replay = await client.post(url + f"/runs/{run_id}/cancel", headers={"Idempotency-Key": "fresh-click-same-run"})
            assert replay.status_code == 200 and replay.json()["replayed"], replay.text
            assert replay.json()["status"] == ("rejected" if unknown_cancel else "applying"), replay.text
            if unknown_cancel:
                assert replay.json()["reason_code"] == "run_terminal_before_cancel_effect", replay.text
            assert len(signals) == 1 and signals[0]["user_id"] == requester_id
            async with sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                assert (await db.get(RuntimeTask, run_id)).status == ("needs_reconciliation" if unknown_cancel else "running")
                rows = list(await db.scalars(select(SessionControlInput).where(SessionControlInput.session_id == session_id)))
                assert len(rows) == 1 and rows[0].status == ("rejected" if unknown_cancel else "applying")
                command = await db.get(SessionCommand, rows[0].command_id)
                assert command.principal_id == requester_id and command.target_json["expected_run_id"] == str(run_id)
            release.set()
            await asyncio.wait_for(asyncio.shield(worker), 30)
            async with sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                task = await db.get(RuntimeTask, run_id)
                control = await db.get(SessionControlInput, rows[0].id)
                assert task.status == ("needs_reconciliation" if unknown_cancel else "killed")
                assert task.metadata_json["terminal_execution_fence_ref"]
                assert control.status == ("rejected" if unknown_cancel else "applied")
                if unknown_cancel:
                    assert task.metadata_json["session_v2_reconciliation"]["delivery_state"] == "unknown"
                events = list(await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.command_id == command.id)))
                kinds = [event.event_type for event in events]
                assert kinds.count("control_input.accepted") == kinds.count("control_input.started") == 1
                assert kinds.count("control_input.applied") == kinds.count("run.cancelled") == (0 if unknown_cancel else 1)
                assert kinds.count("control_input.rejected") == (1 if unknown_cancel else 0)
                (tmp_path / "group-cancel-terminal.json").write_text(json.dumps({"run_status": task.status,
                    "control_status": control.status, "event_kinds": kinds, "metadata": task.metadata_json}, default=str, indent=2))
            assert not (seed["agent_root"] / "workspace/after-stop.md").exists()
            if browser_cancel:
                line = await asyncio.wait_for(process.stdout.readline(), 60)
                assert line and json.loads(line)["event"] == "work_stopped", line
                assert await asyncio.wait_for(process.wait(), 10) == 0
    finally:
        release.set()
        try:
            if worker is not None and not worker.done():
                await asyncio.wait_for(asyncio.shield(worker), 30)
        finally:
            if process is not None:
                if process.returncode is None:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), 10)
                (tmp_path / "cancel-browser-stderr.log").write_bytes(await process.stderr.read())
            if server is not None:
                server.should_exit = True
            try:
                if server_task is not None:
                    await asyncio.wait_for(server_task, 10)
            finally:
                if listener is not None:
                    listener.close()


async def _exercise_group_permission(seed, session_id, run_id, requester_id, sessionmaker, monkeypatch, tmp_path,
    *, decision, model_client, submit, actor, other_actor, browser_permission=False):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app import database
    from app.api import chat_sessions
    from app.core.security import get_current_user
    from app.database import pin_rls_tenant_context
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionControlInput, SessionToolInvocation
    from app.models.user import User
    from app.services.feishu_service import FeishuService
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.session_input_dispatch import recover_admitted_session_inputs_once
    from app.services.web_chat_runtime import execute_web_chat_run

    async with sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        task = await db.get(RuntimeTask, run_id)
        assert task.status == "suspended", (task.status, task.result_summary, task.metadata_json)
        waiting = list(await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == run_id,
            SessionToolInvocation.permission_state == "waiting")))
        assert len(waiting) == 1 and waiting[0].result_event_id is None and waiting[0].effect_state == "prepared_not_started"
        invocation_id, permission_id = waiting[0].id, waiting[0].permission_item_id
    target = seed["agent_root"] / "workspace/shared-report.md"
    assert not target.exists() and len(model_client.requests) == 1
    reader_id = requester_id
    async def current_user():
        async with sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            return await db.get(User, reader_id)
    async def scoped_db():
        async with sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db
    web = FastAPI()
    web.include_router(chat_sessions.router)
    web.dependency_overrides[get_current_user] = current_user
    web.dependency_overrides[database.get_db] = scoped_db
    url = f"/agents/{seed['agent_id']}/sessions/{session_id}"
    resolve_url = url + f"/permissions/{permission_id}/resolve"
    async with AsyncClient(transport=ASGITransport(app=web), base_url="http://group.fixture") as client:
        for reader_id in (requester_id, seed["user_id"]):
            history = await client.get(url + "/transcript", params={"schema_version": 2, "limit": 1000})
            assert history.status_code == 200 and str(permission_id) in history.text and "GROUP_REPORT" in history.text, history.text
            assert f'"requested_by_user_id":"{requester_id}"' in history.text
        denied = await client.post(resolve_url, json={"action": decision})
        assert denied.status_code == 409 and denied.json()["detail"]["code"] == "shared_group_requester_conflict", denied.text
        reader_id = requester_id
        for status, members in [(403, set()), (503, None)]:
            async def unavailable_members(*_args, **_kwargs):
                if members is None:
                    raise RuntimeError("Synthetic membership outage")
                return members
            with monkeypatch.context() as patch:
                patch.setattr(FeishuService, "get_chat_member_open_ids", unavailable_members)
                unavailable = await client.post(resolve_url, json={"action": decision})
                assert unavailable.status_code == status, unavailable.text
            assert not target.exists()
        # A mismatched stored effective payload cannot reveal or execute a stale request.
        async with sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            invocation = await db.get(SessionToolInvocation, invocation_id)
            original = invocation.effective_arguments_json
            invocation.effective_arguments_json = {**original, "content": "MISMATCHED_PERMISSION"}
            await db.commit()
        unavailable = await client.post(resolve_url, json={"action": decision})
        assert unavailable.status_code == 503, unavailable.text
        async with sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            (await db.get(SessionToolInvocation, invocation_id)).effective_arguments_json = original
            assert list(await db.scalars(select(SessionControlInput).where(SessionControlInput.expected_run_id == run_id))) == []
            await db.commit()
        if browser_permission:
            reader_id = seed["user_id"]
            async def requester_ready():
                nonlocal reader_id
                reader_id = requester_id
            async with _group_permission_browser(web, seed, session_id, permission_id, requester_id, monkeypatch,
                tmp_path, requester_ready) as recorded:
                receipt = recorded
                assert receipt["run_id"] == str(run_id)
        else:
            response = await client.post(resolve_url, json={"action": decision})
            assert response.status_code == 200, response.text
            receipt = response.json()
        (tmp_path / "group-permission-response.json").write_text(json.dumps(receipt))
        assert receipt["status"] == "resolved" and receipt["run_id"] == str(run_id) and receipt["run_status"] == "resumable"
        assert receipt["result_event_id"] and receipt["recovery_action"] == "resume_same_runtime_task"
        history = await client.get(url + "/transcript", params={"schema_version": 2, "limit": 1000})
        assert history.status_code == 200, history.text
        decisions = [item for item in history.json() if item.get("item_kind") == "control_input"
            and item.get("item_id") == receipt["control_id"]]
        assert len(decisions) == 2 and all(item["source_access_receipt"]["status"] == "allowed" for item in decisions)
        replay = await client.post(resolve_url, json={"action": decision})
        assert replay.status_code == 200 and replay.json()["control_id"] == receipt["control_id"], replay.text
        conflict = await client.post(resolve_url, json={"action": "deny" if decision != "deny" else "allow_once"})
        assert conflict.status_code == 409, conflict.text
    assert (target.read_text() == "GROUP_REPORT") if decision != "deny" else not target.exists()
    async with sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        controls = list(await db.scalars(select(SessionControlInput).where(SessionControlInput.expected_run_id == run_id)))
        assert len(controls) == 1 and controls[0].status == "applied"
        grants = ((await db.get(ChatSession, session_id)).transcript_metadata_json or {}).get("session_permission_grants", [])
        assert len(grants) == (1 if decision == "allow_session" else 0)
        if grants:
            assert grants[0]["principal_id"] == str(requester_id) and grants[0]["principal_type"] == "user"
    async def resume(expected_run):
        async with sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            claims = await RuntimeTaskClaimService(db=db, worker_id="permission-worker", lease_seconds=600).claim_available()
            claimed = next(row for row in claims if row.id == expected_run)
        fence = set_runtime_task_fence(task_id=expected_run, worker_id="permission-worker", claim_version=claimed.claim_version)
        try:
            await execute_web_chat_run(expected_run)
        finally:
            reset_runtime_task_fence(fence)
        async with sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            current = await db.get(RuntimeTask, expected_run)
        if current.status == "completed":
            from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
            from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

            processor = WebTerminalBoundaryProcessor(session_factory=sessionmaker, data_root=seed["data_root"])
            terminal = await RuntimeTerminalBoundaryOutboxService(session_factory=sessionmaker).drain_once(
                tenant_id=seed["tenant_id"], worker_id="permission-terminal", canonical_validator=processor.validate,
                process_callback=processor, task_types=("web_chat_turn",))
            assert terminal["delivered"] >= 1, terminal
        return current
    if decision == "deny":
        model_client.script = [{"content": "The write was denied; no file was created."}]
    task = await resume(run_id)
    assert task.status == "completed", (task.status, task.result_summary, task.metadata_json)
    assert len(model_client.requests) == (2 if decision == "deny" else 3)
    async with sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        writes = list(await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == run_id,
            SessionToolInvocation.tool_name == "write_file")))
        assert len(writes) == 1 and writes[0].id == invocation_id and writes[0].result_event_id
        assert writes[0].permission_state == ("denied" if decision == "deny" else "approved")
    if decision == "allow_session":
        # The same payload is reusable by A; a later B turn must ask B independently.
        for sender, subject, must_wait in [(actor, "ou_group_requester", False), (other_actor, "ou_group_creator", True)]:
            if must_wait:
                from app.core.resource_authority import workspace_resource_id
                from app.models.security_audit import ResourcePermission

                async with sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    db.add(ResourcePermission(tenant_id=seed["tenant_id"], principal_type="user", principal_id=sender.id,
                        resource_type="workspace_file", resource_id=workspace_resource_id(seed["agent_id"], "workspace/shared-report.md"),
                        actions=["read", "write"], effect="allow", conditions={}))
                    await db.commit()
            event, accepted = await submit(sender, subject, "Write the same report again.", f"grant-{subject}")
            command_id = UUID(accepted["command_id"])
            async with sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                await recover_admitted_session_inputs_once(db, worker_id="grant-dispatch", tenant_id=seed["tenant_id"],
                    admission_ids=(uuid5(command_id, "input-admission:revision:1"),))
                next_run = await db.scalar(select(RuntimeTask).where(RuntimeTask.metadata_json["session_v2_input_id"].astext == str(event.event_id)))
                assert next_run is not None and next_run.id != run_id, accepted
            model_client.script = [{"tool_calls": [{"id": f"grant-{subject}", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps(original)}}], "finish_reason": "tool_calls"},
                {"content": "The same report is ready."}]
            next_task = await resume(next_run.id)
            assert next_task.status == ("suspended" if must_wait else "completed"), (next_task.status, next_task.result_summary)
            async with sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                invocation = await db.scalar(select(SessionToolInvocation).where(SessionToolInvocation.run_id == next_run.id))
                assert invocation.permission_state == ("waiting" if must_wait else "not_required")
                assert (invocation.result_event_id is None) is must_wait
    (tmp_path / "group-permission-model-requests.json").write_text(json.dumps(model_client.requests, default=str, indent=2))


from contextlib import asynccontextmanager


@asynccontextmanager
async def _group_permission_browser(web, seed, session_id, permission_id, requester_id, monkeypatch, tmp_path, requester_ready):
    import asyncio
    import socket
    from pathlib import Path
    import uvicorn
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from app.api import agents, auth, chat_sessions, websocket as socket_api
    from app.core.security import create_access_token
    from app.services.web_chat_broker import WebChatBroker
    import app.services.web_chat_runtime as runtime

    frontend = Path(__file__).resolve().parents[3] / "frontend"
    assert (frontend / "dist/index.html").is_file()
    broker = WebChatBroker()
    monkeypatch.setattr(runtime, "web_chat_broker", broker)
    monkeypatch.setattr(socket_api, "manager", broker)
    for router in (chat_sessions.router, agents.router, auth.router):
        web.include_router(router, prefix="/api")
    web.include_router(socket_api.router)
    web.mount("/assets", StaticFiles(directory=frontend / "dist/assets"), name="assets")
    @web.get("/api/{path:path}")
    async def unavailable_fixture_api(path: str):
        return JSONResponse(status_code=404, content={"detail": "Unsupported fixture endpoint"})
    @web.get("/{path:path}")
    async def built_frontend(path: str):
        return FileResponse(frontend / "dist/index.html")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(web, host="127.0.0.1", port=port, lifespan="off", access_log=False, log_level="warning"))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    process = None
    try:
        for _ in range(500):
            if server.started:
                break
            assert not server_task.done()
            await asyncio.sleep(0.01)
        assert server.started
        fixture_path = tmp_path / "permission-browser-fixture.json"
        fixture_path.write_text(json.dumps({"origin": f"http://127.0.0.1:{port}", "tenantId": str(seed["tenant_id"]),
            "agentId": str(seed["agent_id"]), "sessionId": str(session_id), "permissionId": str(permission_id),
            "readerToken": create_access_token(str(seed["user_id"]), "user", str(seed["tenant_id"])),
            "requesterToken": create_access_token(str(requester_id), "user", str(seed["tenant_id"])),
            **{key: str(tmp_path / name) for key, name in {"readonlyScreenshot": "permission-readonly.png",
                "unconfirmedScreenshot": "permission-unconfirmed.png", "completedScreenshot": "permission-applied.png",
                "receiptPath": "permission-browser-receipt.json", "errorPath": "permission-browser-error.json",
                "errorScreenshot": "permission-browser-error.png"}.items()}}))
        process = await asyncio.create_subprocess_exec("node", str(frontend / "e2e/group-permission-native.mjs"), str(fixture_path),
            cwd=frontend, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        line = await asyncio.wait_for(process.stdout.readline(), 60)
        assert line and json.loads(line)["event"] == "readonly_checked", line
        await requester_ready()
        process.stdin.write(json.dumps({"event": "requester_ready"}).encode() + bytes([10]))
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), 60)
        assert line and json.loads(line)["event"] == "permission_recorded", line
        yield json.loads((tmp_path / "permission-browser-receipt.json").read_text())
        stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        assert process.returncode == 0, (stdout, stderr)
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        server.should_exit = True
        await asyncio.wait_for(server_task, 10)
        listener.close()
