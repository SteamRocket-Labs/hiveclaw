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


@pytest.mark.parametrize("mutation", [None, "forged_actor", "unlinked", "private_source"])
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
    from app.services.channel_session import find_or_create_shared_group_session
    from app.services.external_principal_service import bind_authenticated_self_channel_principal
    from app.services.feishu_service import FeishuService
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.secrets_provider import get_secrets_provider
    from app.services.session_live_input import submit_live_human_input
    from app.services.session_input_dispatch import recover_admitted_session_inputs_once
    from app.services.web_chat_runtime import execute_web_chat_run

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
        grant.actions = ["read", "execute"]
        shared = await find_or_create_shared_group_session(db, tenant_id=seed["tenant_id"], agent_id=agent.id,
            installation_ref=str(config.id), chat_id="oc_native_group", first_message_title="Shared report")
        shared_id, installation = shared.id, str(config.id)
        await db.commit()

    async def members(_self, *_args, **_kwargs):
        return {"ou_group_requester"}
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
    prompt = "Write workspace/shared-report.md with GROUP_REPORT, read it back, and report."
    body = {"event": {"sender": {"sender_id": {"open_id": "ou_group_requester"}}, "message": {
        "chat_type": "group", "chat_id": "oc_native_group", "message_type": "text", "content": json.dumps({"text": prompt})}}}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        event = await accept_authenticated_channel_event(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            provider="feishu", installation_ref=installation, provider_event_id="native-shared-work",
            handler_key="feishu.event", body=body)
    assert any(item.id == event.event_id for item in await ChannelIngressInboxService(
        session_factory=app_user_sessionmaker).claim_batch(worker_id="shared-ingress"))
    with use_channel_ingress_context(event_id=event.event_id, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
        provider="feishu", installation_ref=installation):
        bind_channel_ingress_external_principal(actor.external_principal_id)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            session = await db.get(ChatSession, shared_id)
            db.add(ChatMessage(tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], user_id=member.id,
                external_principal_id=actor.external_principal_id, conversation_id=str(shared_id), role="user", content=prompt))
            await db.flush()
            accepted = await submit_live_human_input(db=db, agent=await db.get(Agent, seed["agent_id"]), user=actor,
                session=session, source="feishu", content=prompt, input_id=event.event_id, idempotency_key="native-shared-work",
                runtime_metadata={"source": "feishu", "channel": "feishu"})
            command_id = UUID(accepted["command_id"])
            ingress = await db.get(ChannelIngressEvent, event.event_id)
            ingress.status, ingress.external_principal_id = "processed", actor.external_principal_id
            await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        await recover_admitted_session_inputs_once(db, worker_id="shared-dispatch", tenant_id=seed["tenant_id"],
            admission_ids=(uuid5(command_id, "input-admission:revision:1"),))
        task = await db.scalar(select(RuntimeTask).where(RuntimeTask.metadata_json["session_v2_input_id"].astext == str(event.event_id)))
        assert task is not None, accepted
        run_id = task.id
        assert task.root_user_id == member.id != seed["user_id"]
        assert (await db.get(SessionCommand, command_id)).principal_id == actor.external_principal_id
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
    client = ScriptedLLMClient([
        {"tool_calls": [{"id": "shared-write", "type": "function", "function": {"name": "write_file",
            "arguments": json.dumps({"path": "workspace/shared-report.md", "content": "GROUP_REPORT"})}}], "finish_reason": "tool_calls"},
        {"tool_calls": [{"id": "shared-read", "type": "function", "function": {"name": "read_file",
            "arguments": json.dumps({"path": "workspace/shared-report.md"})}}], "finish_reason": "tool_calls"},
        {"content": "The shared report is ready.", "finish_reason": "stop"},
    ])
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
        if mutation is not None:
            assert task.status != "completed", (task.status, task.result_summary)
            assert client.requests == []
            assert not (seed["agent_root"] / "workspace/shared-report.md").exists()
            return
        assert task.status == "completed", (task.status, task.result_summary, task.metadata_json)
        assert len(client.requests) == 3 and "GROUP_REPORT" in json.dumps(client.requests[-1]["messages"])
        reads = [message for message in client.requests[-1]["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == "shared-read"]
        assert len(reads) == 1 and "GROUP_REPORT" in str(reads[0]["content"]), reads
        assert all(row.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] for row in results)
        assert (await db.get(SessionTurnInput, event.event_id)).status == "applied"
        assert (seed["agent_root"] / "workspace/shared-report.md").read_text() == "GROUP_REPORT"
        spans = list(await db.scalars(select(InvocationSpan).where(InvocationSpan.runtime_task_id == run_id,
            InvocationSpan.span_type == "kernel_context")))
        assert spans and all(span.user_id == member.id for span in spans)
        outbox = list(await db.scalars(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == run_id)))
        assert len(outbox) == 1
        # Prioritize this fixture without claiming unrelated session-scoped test rows.
        outbox[0].available_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        await db.commit()
    sends = []
    async def send(**kwargs):
        sends.append(kwargs)
        return {"synthetic": True}
    outcome = await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker,
        text_sender=send, file_sender=send).drain_once(worker_id="shared-delivery", limit=1)
    assert outcome["claimed"] == 1 and outcome["retried"] == 1, outcome
    assert sends == []
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        delivery = await db.get(ChannelDeliveryOutbox, outbox[0].id)
        assert delivery.status == "pending" and "shared_group_output_unavailable" in delivery.last_error
