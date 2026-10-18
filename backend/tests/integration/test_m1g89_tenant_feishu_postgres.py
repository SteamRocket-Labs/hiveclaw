"""Enterprise installation identity survives real ingress, admission and delivery."""

import json
import os
import subprocess
import sys
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.models.external_principal import ExternalPrincipal
from app.models.identity import ExternalIdentity, IdentityProvider
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.models.tenant_channel_config import TenantChannelConfig
from app.models.user import User
from app.models.user_assistant_binding import UserAssistantBinding
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run, ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_enterprise_identity_migration_preserves_qr_and_refuses_lossy_downgrade(
    migrated_pg_url, owner_sessionmaker, tmp_path,
):
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    def migrate(target):
        return subprocess.run([sys.executable, "-B", "-m", "alembic", *target], env={**os.environ,
            "DATABASE_URL": migrated_pg_url}, capture_output=True, text=True, timeout=60)
    empty = migrate(["downgrade", "employee_readiness_probe_1018"])
    assert empty.returncode == 0, empty.stderr
    principal_id, config_id = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        db.add(ChannelConfig(id=config_id, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            channel_type="feishu", app_id="cli_legacy", app_secret="synthetic-legacy", is_configured=True))
        await db.flush()
        await db.execute(text("INSERT INTO external_principals (id,tenant_id,provider,installation_ref,channel_config_id,"
            "subject_id,display_name,linked_user_id,binding_method,linked_at,binding_verified_at) "
            "VALUES (:id,:tenant,'feishu',:installation,:config,'ou_legacy','Legacy member',:user,'feishu_qr',now(),now())"),
            {"id": principal_id, "tenant": seed["tenant_id"], "installation": str(config_id), "config": config_id, "user": seed["user_id"]})
        await db.commit()
    upgraded = migrate(["upgrade", "head"])
    assert upgraded.returncode == 0, upgraded.stderr
    async with owner_sessionmaker() as db:
        legacy = await db.get(ExternalPrincipal, principal_id)
        assert legacy.channel_config_id == config_id and legacy.tenant_channel_config_id is None
        assert legacy.linked_user_id == seed["user_id"] and legacy.binding_method == "feishu_qr"
        config = TenantChannelConfig(id=uuid4(), tenant_id=seed["tenant_id"], channel_type="feishu", app_id="cli_new",
            app_secret="synthetic-new")
        db.add(config)
        await db.flush()
        db.add(ExternalPrincipal(id=uuid4(), tenant_id=seed["tenant_id"], provider="feishu", installation_ref="tenant-evidence",
            tenant_channel_config_id=config.id, subject_id="ou_new", display_name="New member"))
        await db.commit()
    refused = migrate(["downgrade", "employee_readiness_probe_1018"])
    assert refused.returncode != 0 and "no bindings are deleted or relabeled" in refused.stderr
    async with owner_sessionmaker() as db:
        assert (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "tenant_channel_principal_1018"
        assert (await db.get(ExternalPrincipal, principal_id)).binding_method == "feishu_qr"


async def test_enterprise_installation_normal_text_file_and_original_work_delivery(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.api import feishu, tenant_channels
    from app.config import get_settings
    from app.services.channel_delivery_service import ChannelDeliveryService
    from app.services.channel_ingress_dispatcher import _dispatch_verified_payload, dispatch_channel_ingress_event
    from app.services.channel_ingress_inbox import ChannelIngressInboxService, channel_installation_ref
    from app.services.channel_ingress_context import use_channel_ingress_context
    from app.services.external_principal_service import load_external_runtime_actor
    from app.services.feishu_service import FeishuService
    from app.services.secrets_provider import get_secrets_provider

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    import app.database as database
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.models.capability_policy import CapabilityPolicy
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
    monkeypatch.setattr("random.uniform", lambda *_: 0)
    calls, downloads = [], []
    async def profile(_agent, *, sender_open_id, **_kwargs):
        return {"open_id": sender_open_id, "name": "Verified member"}
    async def send(self, app_id, _secret, receive_id, *_args, **kwargs):
        calls.append((app_id, receive_id, kwargs.get("receive_id_type")))
        return {"data": {"message_id": "synthetic-reply"}}
    async def send_file(self, app_id, _secret, receive_id, path, **_kwargs):
        calls.append((app_id, receive_id, "file"))
        assert path.read_bytes() == b"ENTERPRISE_RESULT"
        return {"data": {"message_id": "synthetic-file-reply"}}
    async def no_card(*_args, **_kwargs):
        return None
    async def download(*_args, **_kwargs):
        downloads.append(True)
        return b"Enterprise input"
    monkeypatch.setattr(feishu, "_resolve_feishu_sender_profile", profile)
    monkeypatch.setattr(FeishuService, "send_message", send)
    monkeypatch.setattr(FeishuService, "upload_and_send_file", send_file)
    monkeypatch.setattr(feishu.feishu_service, "download_message_resource", download)
    for name in ("create_card_entity", "send_card_by_card_id", "patch_message", "set_card_streaming_mode"):
        monkeypatch.setattr(feishu.feishu_service, name, no_card)
    async with owner_sessionmaker() as db:
        owner = await db.get(User, seed["user_id"])
        agent = await db.get(Agent, seed["agent_id"])
        agent.default_session_permission_mode = "bypassPermissions"
        await AgentManager().initialize_agent_files(db, agent, personality="Enterprise assistant",
            original_author_id=owner.id)
        db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="workspace.file.write", allowed=True,
            requires_approval=False))
        config = TenantChannelConfig(id=uuid4(), tenant_id=seed["tenant_id"], channel_type="feishu",
            app_id="cli_enterprise", app_secret="synthetic-enterprise-secret", verification_token="synthetic-token")
        provider = IdentityProvider(id=uuid4(), tenant_id=seed["tenant_id"], provider_type="feishu",
            name="Enterprise identity", is_active=True, config={"app_id": config.app_id})
        decoy = ChannelConfig(id=uuid4(), tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], channel_type="feishu",
            app_id="cli_employee_decoy", app_secret="synthetic-decoy-secret", is_configured=True)
        db.add_all([config, provider, decoy])
        await db.flush()
        identity = ExternalIdentity(id=uuid4(), provider_id=provider.id, user_id=owner.id, provider_open_id="ou_enterprise",
            provider_user_id="enterprise-member")
        db.add_all([identity, UserAssistantBinding(tenant_id=seed["tenant_id"], user_id=owner.id,
            agent_id=seed["agent_id"], status="active", source="explicit_selection", requester_user_id=owner.id,
            owner_user_id_at_bind=owner.id)])
        (await db.get(RuntimeTask, seed["run_id"])).status = "completed"
        model = await db.get(LLMModel, (await db.get(Agent, seed["agent_id"])).primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-tenant-model")
        model.max_input_tokens = 256_000
        await db.commit()
    inbox = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
    web = FastAPI()
    web.include_router(tenant_channels.router, prefix="/api")
    async def ingress_database():
        async with app_user_sessionmaker() as db:
            yield db
            await db.commit()
    web.dependency_overrides[get_db] = ingress_database
    async def deliver(key, kind="text"):
        body = {"header": {"event_id": key, "event_type": "im.message.receive_v1", "token": "synthetic-token"},
            "event": {"sender": {"sender_id": {"open_id": "ou_enterprise", "user_id": "enterprise-member"}},
                "message": {"message_id": key, "message_type": kind, "chat_type": "p2p", "chat_id": "oc_private",
                    "content": json.dumps({"text": "Perform the assignment"} if kind == "text" else
                        {"file_key": key, "file_name": "input.txt"})}}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://test") as client:
            response = await client.post(f"/api/channel/feishu/tenant/{seed['tenant_id']}/webhook", json=body)
        assert response.status_code == 200 and response.json()["routed"] is True
        claimed = next(item for item in await inbox.claim_batch(worker_id="enterprise", limit=10) if item.provider_event_id == key)
        with use_channel_ingress_context(event_id=claimed.id, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            provider="feishu", installation_ref=channel_installation_ref(config, fallback="")) as context:
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                result = await _dispatch_verified_payload(db, claimed)
            assert result is not None and context.external_principal_id is not None
            return claimed, context
    claimed, context = await deliver("enterprise-text")
    assert context.session_id is not None and context.runtime_task_id is not None
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        principal = await db.get(ExternalPrincipal, context.external_principal_id)
        assert principal.channel_config_id is None and principal.tenant_channel_config_id == config.id
        actor = await load_external_runtime_actor(db, tenant_id=seed["tenant_id"], principal_id=principal.id, expected_user_id=owner.id)
        assert actor.authority_bound and actor.id == owner.id
        session = await db.get(ChatSession, context.session_id)
        assert session.user_id == owner.id and session.audience_kind != "group"
        target = dict(session.delivery_target_json)
        assert UUID(target["tenant_channel_config_id"]) == config.id
        message = await db.scalar(select(ChatMessage).where(ChatMessage.source_ingress_event_id == claimed.id))
        assert message.user_id == owner.id and message.conversation_id == str(session.id)
        sent = await ChannelDeliveryService.send_text(db=db, agent_id=seed["agent_id"], tenant_id=seed["tenant_id"],
            reply_target=target, text="Completed the enterprise work")
        assert sent.ok and calls[-1] == (config.app_id, "ou_enterprise", "open_id")
        # Replacing the installation never falls back to the employee's unrelated bot.
        target["tenant_channel_config_id"] = str(uuid4())
        before = list(calls)
        denied = await ChannelDeliveryService.send_text(db=db, agent_id=seed["agent_id"], tenant_id=seed["tenant_id"],
            reply_target=target, text="Do not send through another bot")
        assert not denied.ok and calls == before
        from app.services.channel_delivery_service import channel_delivery_target
        from app.services.agent_tool_domains.feishu_helpers import load_feishu_channel_configuration
        token = channel_delivery_target.set(dict(session.delivery_target_json))
        try:
            assert (await load_feishu_channel_configuration(db, agent_id=seed["agent_id"])).id == config.id
        finally:
            channel_delivery_target.reset(token)
    recovered = await dispatch_channel_ingress_event(claimed)
    assert UUID(recovered["runtime_task_id"]) == context.runtime_task_id
    assert UUID(recovered["external_principal_id"]) == context.external_principal_id
    # The native worker receives the real bound requester and consumes real file tools.
    import app.runtime.invoker as invoker
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import set_runtime_task_fence, reset_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    script = ScriptedLLMClient([
        {"tool_calls": [{"id": "enterprise-write", "type": "function", "function": {"name": "write_file",
            "arguments": json.dumps({"path": "workspace/enterprise-report.md", "content": "ENTERPRISE_RESULT"})}}], "finish_reason": "tool_calls"},
        {"tool_calls": [{"id": "enterprise-read", "type": "function", "function": {"name": "read_file",
            "arguments": json.dumps({"path": "workspace/enterprise-report.md"})}}], "finish_reason": "tool_calls"},
        {"content": "The [enterprise report](workspace/enterprise-report.md) is ready.", "finish_reason": "stop"},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: script)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        claims = await RuntimeTaskClaimService(db=db, worker_id="enterprise-worker", lease_seconds=600).claim_available()
        claim = next(item for item in claims if item.id == context.runtime_task_id)
        await db.commit()
    fence = set_runtime_task_fence(task_id=claim.id, worker_id="enterprise-worker", claim_version=claim.claim_version)
    try:
        await execute_web_chat_run(claim.id)
    finally:
        reset_runtime_task_fence(fence)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        task = await db.get(RuntimeTask, claim.id)
        assert task.status == "completed" and task.root_user_id == owner.id, (task.status, task.result_summary)
        outbox = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == task.id))
        assert outbox is not None and outbox.channel_config_id == config.id
        assert UUID(outbox.delivery_target_json["tenant_channel_config_id"]) == config.id
    assert (seed["agent_root"] / "workspace/enterprise-report.md").read_text() == "ENTERPRISE_RESULT"
    assert len(script.requests) == 3 and "ENTERPRISE_RESULT" in json.dumps(script.requests[-1]["messages"])
    service = ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker)
    await service.drain_once(worker_id="enterprise-results", limit=10)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        outbox = await db.get(ChannelDeliveryOutbox, outbox.id)
        assert outbox.delivery_receipts_json["text"]["state"] == "delivered"
        assert (config.app_id, "ou_enterprise", "open_id") in calls
        assert (config.app_id, "ou_enterprise", "file") in calls
    _, file_context = await deliver("enterprise-file", "file")
    assert file_context.session_id == context.session_id and downloads == [True]
    from app.core.security import get_current_user
    from app.services.external_principal_service import unlink_external_principal
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        await unlink_external_principal(db, tenant_id=seed["tenant_id"], principal_id=context.external_principal_id,
            actor_user_id=owner.id, reason="Explicitly disconnect enterprise identity")
        await db.commit()
    with pytest.raises(PermissionError, match="explicit_reconnect_required"):
        await deliver("enterprise-unlinked")
    async def authenticated_member():
        return owner
    web.dependency_overrides[get_current_user] = authenticated_member
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://test") as client:
        unknown = await client.post("/api/tenant-channels/feishu/identity/reconnect", json={"principal_id": str(uuid4())})
        assert unknown.status_code == 409
        restored = await client.post("/api/tenant-channels/feishu/identity/reconnect",
            json={"principal_id": str(context.external_principal_id)})
        assert restored.status_code == 200 and restored.json()["authority_bound"] is True
        assert UUID(restored.json()["linked_user_id"]) == owner.id
    async with owner_sessionmaker() as db:
        await db.delete(await db.get(ExternalIdentity, identity.id))
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        actor = await load_external_runtime_actor(db, tenant_id=seed["tenant_id"], principal_id=context.external_principal_id,
            expected_user_id=owner.id)
        assert not actor.authority_bound and actor.id is None
    with pytest.raises(PermissionError, match="sender binding changed"):
        await dispatch_channel_ingress_event(claimed)


async def test_enterprise_webhook_requires_authentication_and_current_assistant(
    owner_sessionmaker, app_user_sessionmaker, tmp_path,
):
    from app.api import tenant_channels
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    async with owner_sessionmaker() as db:
        config = TenantChannelConfig(id=uuid4(), tenant_id=seed["tenant_id"], channel_type="feishu",
            app_id="cli_unprotected", app_secret="synthetic-only")
        db.add(config)
        await db.commit()
    async def database():
        async with app_user_sessionmaker() as db:
            yield db
    app = FastAPI()
    app.include_router(tenant_channels.router, prefix="/api")
    app.dependency_overrides[get_db] = database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/channel/feishu/tenant/{seed['tenant_id']}/webhook", json={"challenge": "forged"})
        assert response.status_code == 403
        async with owner_sessionmaker() as db:
            stored = await db.get(TenantChannelConfig, config.id)
            stored.verification_token = "synthetic-challenge-token"
            provider = IdentityProvider(id=uuid4(), tenant_id=seed["tenant_id"], provider_type="feishu", name="Other app",
                is_active=True, config={"app_id": "cli_other_app"})
            db.add(provider)
            await db.flush()
            db.add(ExternalIdentity(provider_id=provider.id, user_id=seed["user_id"], provider_open_id="ou_same_spelling"))
            await db.commit()
        url = f"/api/channel/feishu/tenant/{seed['tenant_id']}/webhook"
        body = {"header": {"token": "synthetic-challenge-token", "app_id": config.app_id,
            "event_type": "im.message.receive_v1", "event_id": "cross-app"}, "event": {
                "sender": {"sender_id": {"open_id": "ou_same_spelling"}}, "message": {"message_type": "text"}}}
        cross_app = await client.post(url, json=body)
        assert cross_app.status_code == 200 and cross_app.json()["routed"] is False
        async with owner_sessionmaker() as db:
            (await db.get(IdentityProvider, provider.id)).config = {"app_id": config.app_id}
            other = User(id=uuid4(), tenant_id=seed["tenant_id"], username=f"other-{uuid4().hex}",
                email=f"{uuid4().hex}@tenant.example.com", password_hash="x", display_name="Other member")
            db.add(other)
            await db.flush()
            db.add(ExternalIdentity(provider_id=provider.id, user_id=other.id, provider_user_id="another-member"))
            await db.commit()
        from app.services.tenant_feishu import resolve_tenant_feishu_identity
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            current = await db.get(TenantChannelConfig, config.id)
            proof = await resolve_tenant_feishu_identity(db, config=current, sender={"open_id": "ou_same_spelling"})
            assert proof is not None and proof[0].id == seed["user_id"]
            assert await resolve_tenant_feishu_identity(db, config=current,
                sender={"open_id": "ou_same_spelling", "user_id": "another-member"}) is None
        # Even a verified member's sole owned employee is not an assistant selection.
        unbound = await client.post(url, json=body)
        assert unbound.status_code == 200 and unbound.json()["routed"] is False
        body["header"]["app_id"] = "cli_wrong_callback"
        assert (await client.post(url, json=body)).status_code == 403
