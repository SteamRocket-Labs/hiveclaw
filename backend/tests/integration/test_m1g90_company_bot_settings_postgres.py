"""Company settings and member recovery use the same live bot installation."""

from uuid import uuid4
import os

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.external_principal import ExternalPrincipal
from app.models.identity import ExternalIdentity, IdentityProvider
from app.models.tenant_channel_config import TenantChannelConfig
from app.models.user import User
from app.models.user_assistant_binding import UserAssistantBinding
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_company_settings_and_member_disconnect_reconnect_keep_scope_and_saved_secrets(
    owner_sessionmaker, app_user_sessionmaker, tmp_path, monkeypatch,
):
    from app.api import tenant_channels
    from app.services.channel_ingress_inbox import channel_installation_ref
    from app.services.external_principal_service import _link_verified_external_principal, resolve_or_create_external_principal

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    async with owner_sessionmaker() as db:
        owner = await db.get(User, seed["user_id"])
        owner.role = "org_admin"
        member = User(id=uuid4(), tenant_id=seed["tenant_id"], username=f"settings-{uuid4().hex}",
            email=f"{uuid4().hex}@settings.example.com", display_name="Another member", password_hash="x")
        provider = IdentityProvider(id=uuid4(), tenant_id=seed["tenant_id"], provider_type="feishu", name="Company identity",
            config={"app_id": "cli_settings"}, is_active=True)
        db.add_all([member, provider])
        await db.flush()
        db.add_all([ExternalIdentity(provider_id=provider.id, user_id=owner.id, provider_open_id="ou_settings"),
            UserAssistantBinding(tenant_id=seed["tenant_id"], user_id=owner.id, agent_id=seed["agent_id"],
                requester_user_id=owner.id, status="active", source="explicit_selection", owner_user_id_at_bind=owner.id)])
        await db.commit()
    reader = owner
    async def identity():
        return reader
    async def database():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, reader.tenant_id)
            yield db
            await db.commit()
    web = FastAPI()
    web.include_router(tenant_channels.router, prefix="/api")
    web.dependency_overrides[get_current_user] = identity
    web.dependency_overrides[get_db] = database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://test") as client:
        assert (await client.get("/api/tenant-channels/feishu/identity")).json()["state"] == "company_bot_unconfigured"
        saved = await client.put("/api/tenant-channels/feishu", json={"expected_revision": "unconfigured", "app_id": "cli_settings",
            "app_secret": "synthetic-settings-secret", "verification_token": "synthetic-settings-callback", "is_active": True})
        assert saved.status_code == 200
        current = saved.json()
        assert current["has_app_secret"] and current["has_verification_token"] and not current["has_encrypt_key"]
        assert current["revision"] != "unconfigured" and "synthetic-settings" not in saved.text
        wrong_scope = await client.put("/api/tenant-channels/feishu", json={"app_id": "cli_wrong_scope",
            "expected_user_id": str(uuid4()), "app_secret": "synthetic-wrong-scope"})
        assert wrong_scope.status_code == 409
        wrong_company = await client.put("/api/tenant-channels/feishu", json={"app_id": "cli_wrong_scope",
            "expected_tenant_id": str(uuid4()), "app_secret": "synthetic-wrong-scope"})
        assert wrong_company.status_code == 403
        fresh = await client.get("/api/tenant-channels")
        assert fresh.headers["cache-control"] == "no-store" and fresh.json()[0]["revision"] == current["revision"]
        assert (await client.get("/api/tenant-channels/feishu/identity")).json()["state"] == "awaiting_first_message"
        async with owner_sessionmaker() as db:
            assert await db.scalar(select(func.count()).select_from(ExternalPrincipal).where(ExternalPrincipal.tenant_id == seed["tenant_id"])) == 0
        paused = await client.put("/api/tenant-channels/feishu", json={"expected_revision": current["revision"],
            "app_id": "cli_settings", "is_active": False})
        assert paused.status_code == 200 and paused.json()["is_active"] is False
        assert paused.json()["revision"] != current["revision"]
        assert (await client.get("/api/tenant-channels/feishu/identity")).json()["state"] == "company_bot_paused"
        conflict = await client.put("/api/tenant-channels/feishu", json={"expected_revision": current["revision"],
            "app_id": "cli_stale", "app_secret": "synthetic-conflicting-secret"})
        assert conflict.status_code == 409
        enabled = await client.put("/api/tenant-channels/feishu", json={"expected_revision": paused.json()["revision"],
            "app_id": "cli_settings", "is_active": True, "platform_region": "feishu_cn"})
        assert enabled.status_code == 200
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            config = await db.scalar(select(TenantChannelConfig).where(TenantChannelConfig.tenant_id == seed["tenant_id"]))
            assert config.app_secret == "synthetic-settings-secret" and config.verification_token == "synthetic-settings-callback"
            result = await resolve_or_create_external_principal(db, tenant_id=seed["tenant_id"], provider="feishu",
                installation_ref=channel_installation_ref(config, fallback=""), tenant_channel_config_id=config.id,
                subject_id="ou_settings", display_name="Settings member")
            await _link_verified_external_principal(db, tenant_id=seed["tenant_id"], principal_id=result.principal.id,
                user_id=owner.id, actor_user_id=owner.id, binding_method="feishu_tenant_identity", reason="Verified native fixture")
            principal_id = result.principal.id
            from app.models.chat_session import ChatSession
            from app.models.audit import ChatMessage
            private = ChatSession(id=uuid4(), tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], user_id=owner.id,
                external_principal_id=principal_id, source_channel="feishu", external_conv_id="private-settings-history")
            db.add(private)
            await db.flush()
            db.add(ChatMessage(id=uuid4(), tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], user_id=owner.id,
                external_principal_id=principal_id, conversation_id=str(private.id), role="user", content="PRIVATE_SETTINGS_CANARY"))
            await db.commit()
        bound = await client.get("/api/tenant-channels/feishu/identity")
        assert bound.status_code == 200 and bound.json()["state"] == "bound"
        assert bound.json()["principal_id"] == str(principal_id) and bound.json()["user_id"] == str(owner.id)
        pause_link = await client.put("/api/tenant-channels/feishu", json={"expected_revision": enabled.json()["revision"],
            "app_id": "cli_settings", "is_active": False})
        assert pause_link.status_code == 200
        paused_identity = await client.get("/api/tenant-channels/feishu/identity")
        assert paused_identity.json()["state"] == "company_bot_paused"
        assert paused_identity.json()["disconnectable"][0]["principal_id"] == str(principal_id)
        assert (await client.post("/api/tenant-channels/feishu/identity/disconnect", json={"principal_id": str(principal_id)})).status_code == 200
        enabled = await client.put("/api/tenant-channels/feishu", json={"expected_revision": pause_link.json()["revision"],
            "app_id": "cli_settings", "is_active": True})
        assert enabled.status_code == 200
        assert (await client.post("/api/tenant-channels/feishu/identity/reconnect", json={"principal_id": str(principal_id)})).status_code == 200
        reader = member
        assert (await client.get("/api/tenant-channels")).status_code == 403
        own = await client.get("/api/tenant-channels/feishu/identity")
        assert own.status_code == 200 and str(principal_id) not in own.text and own.json()["state"] == "identity_required"
        denied = await client.post("/api/tenant-channels/feishu/identity/disconnect", json={"principal_id": str(principal_id)})
        assert denied.status_code == 403
        reader = owner
        disconnected = await client.post("/api/tenant-channels/feishu/identity/disconnect", json={"principal_id": str(principal_id)})
        assert disconnected.status_code == 200
        assert (await client.get("/api/tenant-channels/feishu/identity")).json()["state"] == "reconnect_required"
        async with owner_sessionmaker() as db:
            from app.models.security_audit import ResourcePermission
            proof = await db.scalar(select(ExternalIdentity).where(ExternalIdentity.provider_id == provider.id))
            proof.user_id = member.id
            db.add(ResourcePermission(tenant_id=seed["tenant_id"], principal_type="user", principal_id=member.id,
                resource_type="agent", resource_id=seed["agent_id"], actions=["read", "execute"], effect="allow"))
            await db.commit()
        reader = member
        inherited = await client.post("/api/tenant-channels/feishu/identity/reconnect", json={"principal_id": str(principal_id)})
        async with owner_sessionmaker() as db:
            private_owner = (await db.get(ChatSession, private.id)).user_id
        assert inherited.status_code == 409, {"response": inherited.json(), "private_session_user_id": str(private_owner)}
        assert private_owner is None
        scoped = await client.get("/api/tenant-channels/feishu/identity")
        assert scoped.json()["state"] == "identity_conflict" and str(principal_id) not in scoped.text
        async with owner_sessionmaker() as db:
            (await db.scalar(select(ExternalIdentity).where(ExternalIdentity.provider_id == provider.id))).user_id = owner.id
            await db.commit()
        reader = owner
        assert (await client.post("/api/tenant-channels/feishu/identity/reconnect", json={"principal_id": str(principal_id)})).status_code == 200
        assert (await client.get("/api/tenant-channels/feishu/identity")).json()["state"] == "bound"
    async with owner_sessionmaker() as db:
        assert (await db.get(ExternalPrincipal, principal_id)).linked_user_id == owner.id
    if os.environ.get("HIVE_G90_DOM_BRIDGE"):
        from app.models.runtime_task import RuntimeTask
        async with owner_sessionmaker() as db:
            runs_before = await db.scalar(select(func.count()).select_from(RuntimeTask).where(RuntimeTask.tenant_id == seed["tenant_id"]))
        await _exercise_dom_bridge(web, seed, tmp_path)
        async with owner_sessionmaker() as db:
            stored = await db.scalar(select(TenantChannelConfig).where(TenantChannelConfig.tenant_id == seed["tenant_id"]))
            assert stored.app_secret == "synthetic-dom-company-secret"
            assert (await db.get(ExternalPrincipal, principal_id)).linked_user_id == owner.id
            assert await db.scalar(select(func.count()).select_from(RuntimeTask).where(RuntimeTask.tenant_id == seed["tenant_id"])) == runs_before
        from app.services.channel_delivery_service import ChannelDeliveryService
        from app.services.feishu_service import FeishuService
        calls = []
        async def send(_self, app_id, secret, receiver, *_args, **_kwargs):
            calls.append((app_id, secret, receiver))
            return {"data": {"message_id": "synthetic-ui-config-consumed"}}
        monkeypatch.setattr(FeishuService, "send_message", send)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            outcome = await ChannelDeliveryService.send_text(db=db, agent_id=seed["agent_id"], tenant_id=seed["tenant_id"],
                reply_target={"channel": "feishu", "receive_id": "ou_settings", "receive_id_type": "open_id", "chat_type": "p2p",
                    "external_principal_id": str(principal_id), "tenant_channel_config_id": str(stored.id),
                    "installation_ref": channel_installation_ref(stored, fallback="")}, text="UI configuration receipt")
            assert outcome.ok
        assert calls == [("cli_settings", "synthetic-dom-company-secret", "ou_settings")]


async def _exercise_dom_bridge(web, seed, tmp_path):
    import asyncio
    import json
    import socket
    from pathlib import Path
    import uvicorn

    root = Path(__file__).resolve().parents[3] / "frontend"
    config = tmp_path / "dom-vitest.config.mjs"
    config.write_text("export default " + json.dumps({"root": str(root), "envDir": str(tmp_path),
        "esbuild": {"jsx": "automatic"}, "test": {"include": ["e2e/tenant-feishu-settings-native.tsx"],
            "environment": "jsdom", "fileParallelism": False, "maxWorkers": 1, "minWorkers": 1,
            "testTimeout": 20000, "cache": False}}) + ";\n")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(web, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    process = None
    try:
        for _ in range(100):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.02)
        assert server.started
        env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
        env.update(NODE_OPTIONS="--no-experimental-webstorage", HIVE_G90_NATIVE_URL=f"http://127.0.0.1:{port}",
            HIVE_G90_USER_ID=str(seed["user_id"]), HIVE_G90_TENANT_ID=str(seed["tenant_id"]))
        process = await asyncio.create_subprocess_exec("node", str(root / "node_modules/vitest/vitest.mjs"), "run",
            "--config", str(config), cwd=root, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output, _ = await asyncio.wait_for(process.communicate(), 90)
        (tmp_path / "dom-native.log").write_bytes(output)
        assert process.returncode == 0, output.decode()[-6000:]
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()
