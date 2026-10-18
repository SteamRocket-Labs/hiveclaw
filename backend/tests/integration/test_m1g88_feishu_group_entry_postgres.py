"""Normal authenticated Feishu text/file ingress consumes explicit shared entry."""

import json
import os
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

from app.database import get_db, pin_rls_tenant_context
from app.core.security import get_current_user
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.security_audit import ResourcePermission
from app.models.session_v2 import SessionCommand
from app.models.user import User
from app.models.workspace_resource import WorkspaceResourceManifest
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_fast_dispatch_targets_its_input_instead_of_another_sessions_waiter(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from uuid import UUID, uuid5
    from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
    from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority
    from app.services.session_human_input import cancel_unbound_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_input_dispatch import dispatch_admitted_input_fast_path

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        user = await db.get(User, seed["user_id"])
        session = ChatSession(id=uuid4(), tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], user_id=user.id)
        db.add(session)
        await db.flush()
        authority = await resolve_session_mutation_authority(db, user=user, agent_id=seed["agent_id"], session_id=session.id,
            action="mutate_session_input")
        ids = [uuid4(), uuid4()]
        for index, input_id in enumerate(ids):
            await accept_human_input(db, authority=authority, intent={"kind": "start_turn", "input_id": str(input_id),
                "idempotency_key": str(input_id), "session_id": str(session.id), "content_parts": [{"type": "text", "text": "Target work"}]})
            if index == 0:
                await cancel_unbound_human_input(db, authority=authority, input_id=input_id, expected_revision=1)
        admitted = await run_user_prompt_admission(db, authority=authority, input_id=ids[1], worker_id="target-admission")
        target = await db.get(SessionTurnInput, ids[1])
        assert target.queue_ordinal == 2
        unrelated = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.session_id == seed["session_id"]))
        assert unrelated.dispatch_state == "pending" and unrelated.dispatch_attempts == 0
        unrelated_id = unrelated.id
        await db.commit()
        outcome = await dispatch_admitted_input_fast_path(db, admission_id=admitted.admission_id, worker_id="target-fast-path")
        assert outcome.state == "dispatched" and UUID(outcome.receipt["run"]["run_id"]) == uuid5(ids[1], "session-v2-runtime-run")
        replay = await dispatch_admitted_input_fast_path(db, admission_id=admitted.admission_id, worker_id="target-fast-path-replay")
        assert replay.receipt == outcome.receipt
        await db.refresh(unrelated)
        assert unrelated.id == unrelated_id and unrelated.dispatch_state == "pending" and unrelated.dispatch_attempts == 0


async def test_normal_group_text_file_entry_preserves_private_history_and_current_authority(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.api import feishu, chat_sessions
    from app.config import get_settings
    from app.services.channel_ingress_inbox import accept_authenticated_channel_event, ChannelIngressInboxService
    from app.services.channel_ingress_context import use_channel_ingress_context
    from app.services.external_principal_service import resolve_or_create_external_principal
    from app.services.feishu_service import FeishuService
    from app.services.secrets_provider import get_secrets_provider
    from app.models.llm import LLMModel
    from app.models.agent import Agent

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    members = {"ou_owner", "ou_member"}
    async def membership(*_args, **_kwargs):
        return set(members)
    monkeypatch.setattr(FeishuService, "get_chat_member_open_ids", membership)
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.services.tenant_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr("random.uniform", lambda *_: 0)
    downloads, deliveries = [], []
    async def profile(_agent, *, sender_open_id, **_kwargs):
        return {"open_id": sender_open_id, "name": sender_open_id}
    async def download(_app, _secret, message_id, *_args, **_kwargs):
        downloads.append(message_id)
        return ("FILE:" + message_id).encode()
    async def send(*args, **kwargs):
        deliveries.append((args, kwargs))
        return {"data": {"message_id": "fixture-send"}}
    async def no_card(*_args, **_kwargs):
        return None
    monkeypatch.setattr(feishu, "_resolve_feishu_sender_profile", profile)
    monkeypatch.setattr(feishu.feishu_service, "download_message_resource", download)
    monkeypatch.setattr(feishu.feishu_service, "send_message", send)
    for name in ("create_card_entity", "send_card_by_card_id", "patch_message", "set_card_streaming_mode"):
        monkeypatch.setattr(feishu.feishu_service, name, no_card)
    async with owner_sessionmaker() as db:
        owner = await db.get(User, seed["user_id"])
        member = User(id=uuid4(), tenant_id=seed["tenant_id"], username=f"entry-{uuid4().hex}",
            email=f"{uuid4().hex}@entry.example.com", password_hash="x", display_name="Member")
        config = ChannelConfig(id=uuid4(), tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            channel_type="feishu", app_id="fixture-group-entry", app_secret="synthetic-group-entry-secret", is_configured=True)
        db.add_all([member, config])
        await db.flush()
        grant = ResourcePermission(id=uuid4(), tenant_id=seed["tenant_id"], principal_type="user", principal_id=member.id,
            resource_type="agent", resource_id=seed["agent_id"], actions=["read", "execute"], effect="allow")
        db.add(grant)
        principal_ids = {}
        for subject, user in (("ou_owner", owner), ("ou_member", member)):
            resolved = await resolve_or_create_external_principal(db, tenant_id=seed["tenant_id"], provider="feishu",
                installation_ref=str(config.id), channel_config_id=config.id, subject_id=subject, display_name=subject)
            resolved.principal.linked_user_id = user.id
            resolved.principal.binding_method = "feishu_qr"
            resolved.principal.binding_verified_at = resolved.principal.linked_at = datetime.now(timezone.utc)
            principal_ids[subject] = resolved.principal.id
        private = ChatSession(id=uuid4(), tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], user_id=owner.id,
            external_principal_id=principal_ids["ou_owner"], source_channel="feishu", visibility_scope="direct_user",
            external_conv_id="feishu_group_oc_selected_ou_owner")
        db.add(private)
        await db.flush()
        db.add(ChatMessage(id=uuid4(), tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], user_id=owner.id,
            external_principal_id=principal_ids["ou_owner"], conversation_id=str(private.id), role="user", content="PRIVATE_ENTRY_CANARY"))
        (await db.get(RuntimeTask, seed["run_id"])).status = "completed"
        model = await db.get(LLMModel, (await db.get(Agent, seed["agent_id"])).primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-group-entry-model")
        await db.commit()
    reader = owner
    async def identity():
        return reader
    async def database():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, reader.tenant_id)
            yield db
    web = FastAPI()
    web.include_router(feishu.router, prefix="/api")
    web.include_router(chat_sessions.router, prefix="/api")
    web.dependency_overrides[get_current_user] = identity
    web.dependency_overrides[get_db] = database
    inbox = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
    events = {}
    async def deliver(subject, key, *, kind="text", text="Shared assignment", replay=False):
        body = {"header": {"event_type": "im.message.receive_v1", "event_id": key}, "event": {
            "sender": {"sender_id": {"open_id": subject}}, "message": {"message_id": key,
                "message_type": kind, "chat_type": "group", "chat_id": "oc_selected",
                "content": json.dumps({"text": text} if kind == "text" else {"file_key": key, "file_name": "input.txt"})}}}
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            receipt = await accept_authenticated_channel_event(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                provider="feishu", installation_ref=str(config.id), provider_event_id=key, handler_key="feishu.event", body=body)
        if not replay:
            claims = await inbox.claim_batch(worker_id="group-entry", limit=10)
            events[key] = next(item for item in claims if item.id == receipt.event_id)
        with use_channel_ingress_context(event_id=receipt.event_id, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            provider="feishu", installation_ref=str(config.id)) as context:
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                result = await feishu.process_feishu_event(seed["agent_id"], body, db)
            return result, context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://test") as client:
        url = f"/api/agents/{seed['agent_id']}/channel/shared-groups"
        initial = await client.get(url)
        assert initial.status_code == 200 and initial.json()["chat_ids"] == []
        assert initial.headers["cache-control"] == "no-store" and "secret" not in initial.text
        version = initial.json()["revision"]
        if "HIVE_TEST_G88_FRONTEND_DIST" in os.environ:
            await _check_settings_browser(web, seed, owner, tmp_path)
            configured = await client.get(url)
        else:
            configured = await client.put(url, json={"expected_revision": version, "chat_ids": ["oc_selected"]})
        assert configured.status_code == 200 and configured.json()["chat_ids"] == ["oc_selected"]
        assert (await client.put(url, json={"expected_revision": version, "chat_ids": []})).status_code == 409
        assert (await client.put(url, json={"expected_revision": configured.json()["revision"], "chat_ids": ["oc_invalid\n"]})).status_code == 422
        assert (await client.post(f"/api/agents/{seed['agent_id']}/channel", json={"app_id": config.app_id,
            "extra_config": {"shared_group_entry": {"chat_ids": ["oc_selected"]}}})).status_code == 422
        reader = member
        assert (await client.get(url)).status_code == 403
        assert (await client.put(url, json={"expected_revision": configured.json()["revision"], "chat_ids": []})).status_code == 403
        public_channel = await client.get(f"/api/agents/{seed['agent_id']}/channel")
        assert public_channel.status_code == 200 and "shared_group_entry" not in public_channel.text and "oc_selected" not in public_channel.text
        reader = owner
        for subject, key, kind in (("ou_owner", "owner-text", "text"), ("ou_member", "member-file", "file"),
            ("ou_member", "member-text", "text")):
            result, context = await deliver(subject, key, kind=kind)
            assert result["code"] == 0 and context.session_id is not None
            assert context.external_principal_id == principal_ids[subject]
            if key == "owner-text":
                shared_id, first_run = context.session_id, context.runtime_task_id
                assert shared_id != private.id and first_run is not None
                replay_result, replay_context = await deliver(subject, key, replay=True)
                assert replay_result["code"] == 0 and replay_context.session_id == shared_id and replay_context.runtime_task_id == first_run
            assert context.session_id == shared_id
            assert await inbox._mark_processed(item=events[key], worker_id="group-entry", receipt={"status": "accepted",
                "session_id": str(context.session_id), "runtime_task_id": str(context.runtime_task_id) if context.runtime_task_id else None,
                "external_principal_id": str(context.external_principal_id)})
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            shared = await db.get(ChatSession, shared_id)
            assert shared.audience_kind == shared.visibility_scope == "group"
            assert shared.user_id is None and shared.external_principal_id is None
            assert "external_principal_id" not in shared.delivery_target_json and "open_id" not in shared.delivery_target_json
            messages = list(await db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == str(shared_id))))
            assert len([row for row in messages if row.role == "user"]) == 3
            assert {row.user_id for row in messages if row.role == "user"} == {owner.id, member.id}
            commands = list(await db.scalars(select(SessionCommand).where(SessionCommand.session_id == shared_id)))
            assert len(commands) == 2 and {row.principal_id for row in commands} == set(principal_ids.values())
            assert "PRIVATE_ENTRY_CANARY" not in json.dumps([row.request_json for row in commands])
            upload = await db.scalar(select(WorkspaceResourceManifest).where(WorkspaceResourceManifest.agent_id == seed["agent_id"],
                WorkspaceResourceManifest.source == "feishu_upload"))
            assert upload.root_session_id == shared_id and upload.owner_user_id == member.id
            assert (seed["agent_root"] / upload.path).read_bytes() == b"FILE:member-file"
            assert (await db.get(ChatSession, private.id)).external_conv_id == "feishu_group_oc_selected_ou_owner"
            assert await db.scalar(select(ChatMessage.content).where(ChatMessage.conversation_id == str(private.id))) == "PRIVATE_ENTRY_CANARY"
        reader = member
        visible = await client.get(f"/api/agents/{seed['agent_id']}/sessions", params={"scope": "groups"})
        assert visible.status_code == 200 and str(shared_id) in visible.text and str(private.id) not in visible.text
        members.remove("ou_member")
        with pytest.raises(HTTPException) as rejected:
            await deliver("ou_member", "revoked-file", kind="file")
        assert rejected.value.status_code == 403 and downloads == ["member-file"]
        members.add("ou_member")
        reader = owner
        clear = await client.put(url, json={"expected_revision": configured.json()["revision"], "chat_ids": []})
        assert clear.status_code == 200
        result, private_context = await deliver("ou_owner", "private-after-disable")
        assert result["code"] == 0
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            returned_input = await db.scalar(select(ChatMessage).where(
                ChatMessage.source_ingress_event_id == events["private-after-disable"].id))
            assert returned_input.conversation_id == str(private.id) and returned_input.user_id == owner.id
        enabled = await client.put(url, json={"expected_revision": clear.json()["revision"], "chat_ids": ["oc_selected"]})
        assert enabled.status_code == 200
        reset, reset_context = await deliver("ou_owner", "new-group-context", text="/new")
        assert reset["code"] == 0 and reset_context.session_id not in {shared_id, private.id}
        reset_replay, reset_replay_context = await deliver("ou_owner", "new-group-context", text="/new", replay=True)
        assert reset_replay["code"] == 0 and reset_replay_context.session_id == reset_context.session_id
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            old = await db.get(ChatSession, shared_id)
            current = await db.get(ChatSession, reset_context.session_id)
            assert "#archived:" in old.external_conv_id and current.external_conv_id.startswith("feishu_group_shared_")
            assert current.audience_kind == "group" and current.user_id is None and current.external_principal_id is None
            assert len(list(await db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == str(shared_id), ChatMessage.role == "user")))) == 3
            with pytest.raises(PermissionError, match="shared_group_ingress_required"):
                await feishu.process_feishu_event(seed["agent_id"], {"header": {"event_type": "im.message.receive_v1"}, "event": {
                    "sender": {"sender_id": {"open_id": "ou_owner"}}, "message": {"message_type": "text", "chat_type": "group",
                        "chat_id": "oc_selected", "content": json.dumps({"text": "No authenticated inbox"})}}}, db)
    assert "PRIVATE_ENTRY_CANARY" not in json.dumps(deliveries)
    (tmp_path / "group-entry-receipt.json").write_text(json.dumps({"shared_session": str(shared_id),
        "private_session": str(private.id), "first_runtime_task": str(first_run), "real_handler_text_file_members": 2,
        "provider_transport": "synthetic", "authentication": "fixture", "database_role": "rls_app_user"}, indent=2))


async def _check_settings_browser(web, seed, owner, tmp_path):
    import asyncio
    import socket
    from pathlib import Path
    import uvicorn
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from app.api import agents, auth
    from app.core.security import create_access_token

    dist = Path(os.environ["HIVE_TEST_G88_FRONTEND_DIST"])
    assert (dist / "index.html").is_file()
    for router in (agents.router, auth.router):
        web.include_router(router, prefix="/api")
    web.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
    @web.get("/api/{path:path}")
    async def unavailable(path: str):
        return JSONResponse(status_code=404, content={"detail": "Unsupported fixture endpoint"})
    @web.get("/{path:path}")
    async def page(path: str):
        return FileResponse(dist / "index.html")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(web, lifespan="off", access_log=False, log_level="warning"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    process = None
    try:
        for _ in range(500):
            if server.started:
                break
            assert not serving.done()
            await asyncio.sleep(0.01)
        assert server.started
        fixture = {"origin": f"http://127.0.0.1:{listener.getsockname()[1]}", "agentId": str(seed["agent_id"]),
            "tenantId": str(seed["tenant_id"]), "outputDir": str(tmp_path),
            "token": create_access_token(str(owner.id), "user", str(seed["tenant_id"]))}
        path = tmp_path / "browser-fixture.json"
        path.write_text(json.dumps(fixture))
        frontend = Path(__file__).resolve().parents[3] / "frontend"
        process = await asyncio.create_subprocess_exec("node", str(frontend / "e2e/feishu-group-entry-native.mjs"), str(path),
            cwd=frontend, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        output, error = await asyncio.wait_for(process.communicate(), 60)
        assert process.returncode == 0, (output, error)
        assert (tmp_path / "group-browser-receipt.json").is_file()
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()
