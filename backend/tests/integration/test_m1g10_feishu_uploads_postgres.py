"""Provider filenames cannot replace another requester's workspace contents."""

import json
import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.user import User
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_feishu_uploads_keep_sender_ownership_and_never_overwrite_provider_paths(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.api import feishu
    import httpx
    from app.api.files import FileWrite, read_file, write_file
    from app.config import get_settings
    from app.models.agent import AgentPermission
    from app.models.channel_config import ChannelConfig
    from app.models.audit import ChatMessage
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services.channel_ingress_inbox import channel_installation_ref
    from app.services.external_principal_service import resolve_or_create_external_principal
    from app.services.channel_ingress_context import use_channel_ingress_context
    from app.models.channel_ingress_event import ChannelIngressEvent

    tenant, user_id, agent_id, _session, _run = await _seed_session(owner_sessionmaker)
    other_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        other = User(id=other_id, tenant_id=tenant, username=f"upload-{other_id.hex}", email=f"{other_id.hex}@fixture.test",
            password_hash="fixture", display_name="Other uploader", role="member")
        config = ChannelConfig(id=uuid.uuid4(), tenant_id=tenant, agent_id=agent_id, channel_type="feishu",
            app_id="synthetic-upload-app", app_secret="synthetic-upload-app-secret", is_configured=True)
        db.add_all([other, config, AgentPermission(tenant_id=tenant, agent_id=agent_id, scope_type="company", access_level="use")])
        await db.flush()
        for subject, identity in (("ou_first", user_id), ("ou_second", other_id)):
            resolved = await resolve_or_create_external_principal(db, tenant_id=tenant, provider="feishu",
                installation_ref=channel_installation_ref(config, fallback=f"feishu:{agent_id}"), channel_config_id=config.id,
                subject_id=subject, display_name=subject)
            resolved.principal.linked_user_id = identity
            resolved.principal.binding_method = "feishu_qr"
            resolved.principal.linked_at = resolved.principal.binding_verified_at = datetime.now(UTC)
        await db.commit()
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.services.tenant_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("random.uniform", lambda *_: 0)
    outbound = []
    async def no_provider_transport(_transport, request):
        outbound.append(request.url.host)
        raise AssertionError("Provider HTTP is unavailable in this local upload fixture")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_provider_transport)
    acknowledgments = []
    downloads = []
    async def profile(_agent_id, *, sender_open_id, **kwargs):
        return {"open_id": sender_open_id, "name": sender_open_id}
    async def download(_app, _secret, message_id, *args, **kwargs):
        downloads.append(message_id)
        return ("contents:" + message_id).encode()
    async def send(*args, **kwargs):
        acknowledgments.append(args)
    monkeypatch.setattr(feishu, "_resolve_feishu_sender_profile", profile)
    monkeypatch.setattr(feishu.feishu_service, "download_message_resource", download)
    monkeypatch.setattr(feishu.feishu_service, "send_message", send)
    root = tmp_path / str(agent_id)
    (root / "workspace").mkdir(parents=True)
    victim = root / "workspace/victim.txt"
    victim.write_text("Other work must survive")
    async def upload(subject, message_id, filename):
        await feishu._handle_feishu_file(None, agent_id, config, {"message_type": "file", "message_id": message_id,
            "content": json.dumps({"file_key": "resource-" + message_id, "file_name": filename})}, subject, "", "p2p", "")
    ingress_id = uuid.uuid4()
    installation = channel_installation_ref(config, fallback=f"feishu:{agent_id}")
    async with owner_sessionmaker() as db:
        db.add(ChannelIngressEvent(id=ingress_id, tenant_id=tenant, agent_id=agent_id, provider="feishu",
            installation_ref=installation, provider_event_id="synthetic-upload-event", handler_key="feishu.event",
            payload_digest="0" * 64, payload_json={}, status="processing"))
        await db.commit()
    with use_channel_ingress_context(event_id=ingress_id, tenant_id=tenant, agent_id=agent_id, provider="feishu",
        installation_ref=installation) as ingress:
        await upload("ou_first", "message-a", "../victim.txt")
        assert ingress.session_id is not None and ingress.external_principal_id is not None
    assert victim.read_text() == "Other work must survive"
    await upload("ou_second", "message-b", "victim.txt")
    await upload("ou_first", "message-a", "../victim.txt")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        rows = (await db.scalars(select(WorkspaceResourceManifest).where(WorkspaceResourceManifest.agent_id == agent_id,
            WorkspaceResourceManifest.source == "feishu_upload"))).all()
        assert len(rows) == 2 and {row.owner_user_id for row in rows} == {user_id, other_id}
        first = next(row for row in rows if row.owner_user_id == user_id)
        second = next(row for row in rows if row.owner_user_id == other_id)
        assert first.path != second.path and first.root_session_id != second.root_session_id
        assert all(row.path.startswith("workspace/uploads/feishu/") and row.authority_state == "owned" for row in rows)
        from app.services.content_input_receipts import latest_workspace_receipt

        for upload_manifest in rows:
            receipt = await latest_workspace_receipt(db, tenant, agent_id, upload_manifest.path)
            assert receipt is not None and receipt.revision == 1 and receipt.content_sha256 == upload_manifest.content_hash
            assert receipt.dependencies_json == []
        inputs = (await db.scalars(select(ChatMessage).where(ChatMessage.agent_id == agent_id, ChatMessage.role == "user"))).all()
        assert len(inputs) == 2 and {row.user_id for row in inputs} == {user_id, other_id}
        first_input = next(row for row in inputs if row.user_id == user_id)
        assert first_input.source_ingress_event_id == ingress_id and first_input.external_principal_id == ingress.external_principal_id
        loaded = await read_file(agent_id=agent_id, path=first.path, current_user=user, db=db)
        assert loaded.content == "contents:message-a"
        with pytest.raises(HTTPException) as denied:
            await read_file(agent_id=agent_id, path=first.path, current_user=other, db=db)
        assert denied.value.status_code == 403
    assert len(acknowledgments) == 3
    prompts = []
    async def model(_db, _agent, text, **kwargs):
        prompts.append((kwargs["user_id"], text))
        return "Synthetic local reply"
    async def no_card(*args, **kwargs):
        return None
    async def send_card(*args, **kwargs):
        return {"data": {"message_id": "synthetic-card"}}
    monkeypatch.setattr(feishu, "call_agent_llm", model)
    monkeypatch.setattr(feishu.feishu_service, "send_message", send_card)
    for name in ("patch_message", "create_card_entity", "set_card_streaming_mode"):
        monkeypatch.setattr(feishu.feishu_service, name, no_card)
    (root / "workspace/uploads/foreign-most-recent.txt").write_text("Unattributed legacy file")
    for subject in ("ou_first", "ou_second"):
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            response = await feishu.process_feishu_event(agent_id, {"header": {"event_type": "im.message.receive_v1"},
                "event": {"sender": {"sender_id": {"open_id": subject}}, "message": {"message_type": "text", "chat_type": "p2p",
                    "message_id": "followup-" + subject, "content": json.dumps({"text": "Read the file I uploaded."})}}}, db,
                tenant_channel_config=config)
            assert response["code"] == 0, response
    assert prompts[0][0] == user_id and first.path in prompts[0][1] and second.path not in prompts[0][1]
    assert prompts[1][0] == other_id and second.path in prompts[1][1] and first.path not in prompts[1][1]
    assert all("foreign-most-recent" not in text for _, text in prompts)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        response = await feishu.process_feishu_event(agent_id, {"header": {"event_type": "im.message.receive_v1"},
            "event": {"sender": {"sender_id": {"open_id": "ou_first"}}, "message": {"message_type": "post", "chat_type": "p2p",
                "message_id": "post-image-c", "content": json.dumps({"content": [[{"tag": "text", "text": "Inspect this image."},
                    {"tag": "img", "image_key": "post-resource"}]]})}}}, db, tenant_channel_config=config)
        assert response["code"] == 0, response
        images = (await db.scalars(select(WorkspaceResourceManifest).where(WorkspaceResourceManifest.agent_id == agent_id,
            WorkspaceResourceManifest.source == "feishu_upload", WorkspaceResourceManifest.path.endswith("image_resource.jpg")))).all()
        assert len(images) == 1 and images[0].owner_user_id == user_id
        assert images[0].path in prompts[-1][1] and "[image_data:data:image/jpeg;base64," in prompts[-1][1]
        assert (root / images[0].path).read_bytes() == b"contents:post-image-c"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        edited = await write_file(agent_id=agent_id, path=first.path, data=FileWrite(content="Owner revision must survive"), current_user=user, db=db)
        assert edited["status"] == "ok"
        await db.commit()
    with pytest.raises(HTTPException) as changed_upload:
        await upload("ou_first", "message-a", "../victim.txt")
    assert changed_upload.value.status_code == 409
    assert (root / first.path).read_text() == "Owner revision must survive"
    from app.models.external_principal import ExternalPrincipal
    from app.models.chat_session import ChatSession
    from app.services.external_principal_service import load_external_runtime_actor
    from app.services.feishu_uploads import save_feishu_upload, session_upload_references

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        source = await db.get(ChatSession, first.root_session_id)
        actor = await load_external_runtime_actor(db, tenant_id=tenant, principal_id=source.external_principal_id, expected_user_id=user_id)
        assert first.path in await session_upload_references(db, actor=actor, session=source, limit=10)
        with use_channel_ingress_context(event_id=uuid.uuid4(), tenant_id=tenant, agent_id=agent_id, provider="feishu",
            installation_ref="different-installation") as wrong:
            wrong.external_principal_id = actor.external_principal_id
            with pytest.raises(HTTPException) as wrong_installation:
                await save_feishu_upload(db, actor=actor, session=source, message_id="foreign-event", resource_key="foreign-resource",
                    filename="foreign.txt", content=b"must not be written")
            assert wrong_installation.value.status_code == 403
    async with owner_sessionmaker() as db:
        principal = await db.get(ExternalPrincipal, source.external_principal_id)
        principal.status = "revoked"
        await db.commit()
    before = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        with pytest.raises(PermissionError, match="inactive"):
            await save_feishu_upload(db, actor=actor, session=source, message_id="after-revocation", resource_key="new-resource",
                filename="new.txt", content=b"must not be written")
    from app.services.external_principal_service import ExternalPrincipalRevokedError

    download_count = len(downloads)
    with pytest.raises(ExternalPrincipalRevokedError):
        await upload("ou_first", "revoked-provider-read", "new.txt")
    assert len(downloads) == download_count
    assert {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()} == before
    assert outbound == []
