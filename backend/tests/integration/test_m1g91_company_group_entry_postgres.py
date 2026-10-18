"""Company group routes reach shared work without borrowing private assistants."""

import json
import sys
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.models.chat_session import ChatSession
from app.models.channel_config import ChannelConfig
from app.models.identity import ExternalIdentity, IdentityProvider
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.models.security_audit import ResourcePermission
from app.models.tenant_channel_config import TenantChannelConfig
from app.models.user import User
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run, ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_company_group_route_reaches_native_work_and_current_recipients(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import app.database as database_module
    import app.runtime.invoker as invoker
    from app.api import agents, feishu, tenant_channels
    from app.config import get_settings
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    from app.services.channel_ingress_context import use_channel_ingress_context
    from app.services.channel_ingress_dispatcher import _dispatch_verified_payload
    from app.services.channel_ingress_inbox import ChannelIngressInboxService, channel_installation_ref
    from app.services.feishu_service import FeishuService
    from app.services.runtime_task_fence import set_runtime_task_fence, reset_runtime_task_fence
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.secrets_provider import get_secrets_provider
    from app.services.web_chat_runtime import execute_web_chat_run
    from app.services.shared_group_authority import require_current_group_member

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    factory = database_module.async_session
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
    members, deliveries = {"ou_company_owner", "ou_company_member"}, []
    async def membership(*_args, **_kwargs):
        return set(members)
    async def profile(_agent, *, sender_open_id, **_kwargs):
        return {"open_id": sender_open_id, "name": sender_open_id}
    async def send(self, app_id, secret, receiver, *_args, **kwargs):
        deliveries.append((app_id, secret, receiver, kwargs.get("receive_id_type")))
        return {"data": {"message_id": "synthetic-company-group-receipt"}}
    async def download(*_args, **_kwargs):
        return b"Company group input file"
    monkeypatch.setattr(feishu.feishu_service, "download_message_resource", download)
    async def no_card(*_args, **_kwargs):
        return None
    monkeypatch.setattr(FeishuService, "get_chat_member_open_ids", membership)
    monkeypatch.setattr(FeishuService, "send_message", send)
    monkeypatch.setattr(feishu, "_resolve_feishu_sender_profile", profile)
    for name in ("create_card_entity", "send_card_by_card_id", "patch_message", "set_card_streaming_mode"):
        monkeypatch.setattr(feishu.feishu_service, name, no_card)
    async with owner_sessionmaker() as db:
        owner = await db.get(User, seed["user_id"])
        owner.role = "org_admin"
        agent = await db.get(Agent, seed["agent_id"])
        agent.default_session_permission_mode = "bypassPermissions"
        await AgentManager().initialize_agent_files(db, agent, personality="Company group employee", original_author_id=owner.id)
        member = User(id=uuid4(), tenant_id=seed["tenant_id"], username=f"group-{uuid4().hex}",
            email=f"{uuid4().hex}@group.example.com", password_hash="x", display_name="Group member")
        config = TenantChannelConfig(id=uuid4(), tenant_id=seed["tenant_id"], channel_type="feishu",
            app_id="cli_company_group", app_secret="synthetic-company-group-secret", verification_token="synthetic-company-group-token")
        provider = IdentityProvider(id=uuid4(), tenant_id=seed["tenant_id"], provider_type="feishu", name="Company identity",
            config={"app_id": config.app_id}, is_active=True)
        decoy = ChannelConfig(id=uuid4(), tenant_id=seed["tenant_id"], agent_id=agent.id, channel_type="feishu",
            app_id="cli_employee_decoy", app_secret="synthetic-employee-decoy-secret", is_configured=True)
        db.add_all([member, config, provider, decoy])
        await db.flush()
        identities = [ExternalIdentity(id=uuid4(), provider_id=provider.id, user_id=user.id, provider_open_id=subject)
            for user, subject in ((owner, "ou_company_owner"), (member, "ou_company_member"))]
        db.add_all(identities)
        private = ChatSession(id=uuid4(), tenant_id=seed["tenant_id"], agent_id=agent.id, user_id=owner.id,
            source_channel="feishu", external_conv_id="private-company-canary")
        db.add(private)
        await db.flush()
        db.add(ChatMessage(tenant_id=seed["tenant_id"], agent_id=agent.id, user_id=owner.id,
            conversation_id=str(private.id), role="user", content="PRIVATE_COMPANY_GROUP_CANARY"))
        (await db.get(RuntimeTask, seed["run_id"])).status = "completed"
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-company-group-model")
        model.max_input_tokens = 256_000
        await db.commit()
    reader = owner
    async def authenticated():
        return reader
    async def database():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db
            await db.commit()
    web = FastAPI()
    web.include_router(tenant_channels.router, prefix="/api")
    web.include_router(agents.router, prefix="/api")
    web.dependency_overrides[get_db] = database
    web.dependency_overrides[get_current_user] = authenticated
    inbox = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
    route_url = "/api/tenant-channels/feishu/shared-groups"
    route = {"chat_id": "oc_company_shared", "agent_id": str(agent.id)}
    scope = {"expected_user_id": str(owner.id), "expected_tenant_id": str(seed["tenant_id"])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://test") as client:
        initial = await client.get(route_url, params=scope)
        assert initial.status_code == 200, initial.text
        assert initial.headers["cache-control"] == "no-store" and initial.json()["routes"] == []
        saved = await client.put(route_url, json={**scope, "expected_revision": initial.json()["revision"], "routes": [route]})
        assert saved.status_code == 200, saved.text
        assert saved.json()["routes"] == [route] and "synthetic-company-group" not in saved.text
        import os
        if os.environ.get("HIVE_G91_DOM_BRIDGE") == "1":
            from tests.integration.test_m1g90_company_bot_settings_postgres import _exercise_dom_bridge

            await _exercise_dom_bridge(web, seed, tmp_path, entry_file="e2e/tenant-feishu-group-routes-native.tsx")
            saved = await client.get(route_url, params=scope)
            assert saved.json()["routes"] == [route]
        assert (await client.put(route_url, json={**scope, "expected_revision": initial.json()["revision"], "routes": []})).status_code == 409
        assert (await client.put(route_url, json={**scope, "expected_revision": saved.json()["revision"], "routes": [route, route]})).status_code == 422
        assert (await client.put(route_url, json={**scope, "expected_user_id": str(uuid4()), "expected_revision": saved.json()["revision"], "routes": []})).status_code == 409
        async def enqueue(subject, event_id, chat_type="group", kind="text"):
            body = {"header": {"event_id": event_id, "event_type": "im.message.receive_v1", "token": "synthetic-company-group-token"},
                "event": {"sender": {"sender_id": {"open_id": subject}}, "message": {
                    "message_id": event_id, "message_type": kind, "chat_type": chat_type, "chat_id": route["chat_id"],
                    "content": json.dumps({"text": "Summarize our shared assignment"} if kind == "text" else {"file_key": event_id, "file_name": "group-input.txt"})}}}
            response = await client.post(f"/api/channel/feishu/tenant/{seed['tenant_id']}/webhook", json=body)
            assert response.status_code == 200, response.text
            return response
        # Neither member has selected My Assistant; the route is independently explicit.
        assert (await enqueue("ou_company_member", "no-employee-grant")).json()["routed"] is False
        async with owner_sessionmaker() as db:
            grant = ResourcePermission(id=uuid4(), tenant_id=seed["tenant_id"], principal_type="user", principal_id=member.id,
                resource_type="agent", resource_id=agent.id, actions=["read", "execute"], effect="allow")
            db.add(grant)
            await db.commit()
        assert (await enqueue("ou_company_owner", "private-no-assistant", "p2p")).json()["routed"] is False
        contexts, accepted = [], []
        for subject, key in (("ou_company_owner", "company-owner-input"), ("ou_company_member", "company-member-input")):
            assert (await enqueue(subject, key)).json()["routed"] is True
            claimed = next(item for item in await inbox.claim_batch(worker_id="company-group-ingress", limit=10) if item.provider_event_id == key)
            assert claimed.agent_id == agent.id
            accepted.append(claimed)
            with use_channel_ingress_context(event_id=claimed.id, tenant_id=seed["tenant_id"], agent_id=agent.id,
                provider="feishu", installation_ref=channel_installation_ref(config, fallback="")) as context:
                async with app_user_sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed["tenant_id"])
                    await _dispatch_verified_payload(db, claimed)
                contexts.append(context)
        assert contexts[0].session_id == contexts[1].session_id and contexts[0].runtime_task_id
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            shared = await db.get(ChatSession, contexts[0].session_id)
            assert shared.audience_kind == "group" and shared.user_id is None and shared.external_principal_id is None
            assert shared.id != private.id and shared.group_installation_ref == channel_installation_ref(config, fallback="")
            await require_current_group_member(db, session=shared, user=member)
            assert (await db.get(ChatSession, private.id)).user_id == owner.id
        script = ScriptedLLMClient([{"content": "The shared assignment has been summarized.", "finish_reason": "stop"}])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: script)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            claims = await RuntimeTaskClaimService(db=db, worker_id="company-group-worker", lease_seconds=600).claim_available()
            claim = next(item for item in claims if item.id == contexts[0].runtime_task_id)
            await db.commit()
        fence = set_runtime_task_fence(task_id=claim.id, worker_id="company-group-worker", claim_version=claim.claim_version)
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
        assert script.requests and "PRIVATE_COMPANY_GROUP_CANARY" not in json.dumps(script.requests)
        deliveries.clear()
        service = ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker)
        members.add("ou_unknown_recipient")
        held = await service.drain_once(worker_id="company-group-held", limit=10)
        assert held["retried"] == 1 and deliveries == [], held
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            pending = await db.get(ChannelDeliveryOutbox, outbox.id)
            assert pending.status == "pending" and "group_recipient_identity_unavailable" in pending.last_error
            pending.available_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
            await db.commit()
        members.remove("ou_unknown_recipient")
        await service.drain_once(worker_id="company-group-results", limit=10)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            result = await db.get(ChannelDeliveryOutbox, outbox.id)
            assert result.delivery_receipts_json["text"]["state"] == "delivered", result.last_error
        assert deliveries == [(config.app_id, "synthetic-company-group-secret", route["chat_id"], "chat_id")]
        assert (await enqueue("ou_company_member", "company-group-file", kind="file")).json()["routed"] is True
        file_claim = next(item for item in await inbox.claim_batch(worker_id="company-group-file", limit=10) if item.provider_event_id == "company-group-file")
        with use_channel_ingress_context(event_id=file_claim.id, tenant_id=seed["tenant_id"], agent_id=agent.id,
            provider="feishu", installation_ref=channel_installation_ref(config, fallback="")) as file_context:
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                await _dispatch_verified_payload(db, file_claim)
            assert file_context.session_id == shared.id
        removed = await client.put(route_url, json={**scope, "expected_revision": saved.json()["revision"], "routes": []})
        assert removed.status_code == 200
        from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event
        recovered = await dispatch_channel_ingress_event(accepted[0])
        assert UUID(recovered["runtime_task_id"]) == contexts[0].runtime_task_id
        assert UUID(recovered["session_id"]) == shared.id
        assert (await enqueue("ou_company_member", "removed-group-route")).json()["routed"] is False
        members.remove("ou_company_member")
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            with pytest.raises(HTTPException) as denied:
                await require_current_group_member(db, session=shared, user=member)
            assert denied.value.status_code == 403
        members.add("ou_company_member")
        async with owner_sessionmaker() as db:
            await db.delete(await db.get(ExternalIdentity, identities[1].id))
            await db.commit()
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            with pytest.raises(HTTPException) as revoked:
                await require_current_group_member(db, session=shared, user=member)
            assert revoked.value.status_code == 403
        reader = member
        assert (await client.get(route_url, params={**scope, "expected_user_id": str(member.id)})).status_code == 403
