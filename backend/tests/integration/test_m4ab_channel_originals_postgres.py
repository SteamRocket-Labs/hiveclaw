"""Channel original bytes survive work-copy changes under current source authority."""

import hashlib
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from tests.integration.test_m4v_upload_intents_postgres import provider, original_api, delivered, s3_server  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
BODY = b"CHANNEL_ORIGINAL\x00BYTES"


async def save(db, d, channel, message="first"):
    from app.models.chat_session import ChatSession
    from app.models.user import User
    from app.services.feishu_uploads import save_channel_upload

    actor = await db.get(User, d.user.id)
    actor.external_principal_id = None
    return await save_channel_upload(db, actor=actor, session=await db.get(ChatSession, d.session), provider=channel,
        message_id=message, resource_key="synthetic-provider-capability", filename="source.bin", content=BODY)


@pytest.mark.parametrize("channel", ["feishu", "dingtalk", "wecom"])
async def test_channel_bytes_have_stable_private_refs_and_survive_work_copy_changes(
    original_api, provider, app_user_sessionmaker, channel,  # noqa: F811
):
    from app.models.storage_blob import StorageBlob, StorageBlobRef
    from app.models.workspace_resource import WorkspaceResourceManifest

    d, client = original_api
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        first = await save(db, d, channel)
        assert await save(db, d, channel) == first
        second = await save(db, d, channel, "second")
        await db.commit()
    result = await client.get("/chat/attachments/workspace-original", params={"agent_id": str(d.agent), "path": first})
    assert result.status_code == 200, result.text
    ref = result.json()
    assert ref["content_sha256"] == hashlib.sha256(BODY).hexdigest() and ref["size_bytes"] == len(BODY)
    assert "synthetic-provider-capability" not in result.text
    edited = await d.client.put(f"/agents/{d.agent}/files/content", params={"path": first},
        json={"content": "EDITED_WORK_COPY", "expected_current_hash": hashlib.sha256(BODY).hexdigest()})
    assert edited.status_code == 200, edited.text
    (d.root / second).unlink()
    for path in (first, second):
        discovered = await client.get("/chat/attachments/workspace-original", params={"agent_id": str(d.agent), "path": path})
        assert discovered.status_code == 200, discovered.text
        found = discovered.json()
        downloaded = await client.get(f"/chat/attachments/{found['id']}/download")
        assert downloaded.status_code == 200 and downloaded.content == BODY, downloaded.text
    assert (d.root / first).read_bytes() == b"EDITED_WORK_COPY"
    from tests.integration.test_m4ad_original_tool_reads_postgres import context, read

    native = await read(await context(d, app_user_sessionmaker),
        {"workspace_path": first, "original_attachment": ref})
    assert json.loads(str(native))["content"] == BODY.decode()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        rows = (await db.scalars(select(StorageBlob).where(StorageBlob.tenant_id == d.tenant, StorageBlob.kind == "chat_upload"))).all()
        refs = (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.tenant_id == d.tenant, StorageBlobRef.owner_type == "channel_upload"))).all()
        assert len(rows) == 1 and len(refs) == 2 and all(row.blob_id == rows[0].id for row in refs)
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(WorkspaceResourceManifest.agent_id == d.agent,
            WorkspaceResourceManifest.path == first))
        assert manifest.source == "workspace_api_write"
        assert all(row.metadata_json["source"] == f"{channel}_upload" for row in refs)
        assert rows[0].state == "available" and all("synthetic-provider-capability" not in str(row.metadata_json) for row in refs)
    if provider[0] == "s3":
        assert provider[1].writes == 1


async def test_channel_original_discovery_and_download_recheck_current_grants(
    original_api, provider, app_user_sessionmaker, owner_sessionmaker,  # noqa: F811
):
    from app.core.security import create_access_token
    from app.core.resource_authority import workspace_resource_id
    from app.models.agent import AgentPermission
    from app.models.security_audit import ResourcePermission
    from app.models.user import User

    d, client = original_api
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        path = await save(db, d, "feishu")
        await db.commit()
    url = "/chat/attachments/workspace-original"
    params = {"agent_id": str(d.agent), "path": path}
    ref = (await client.get(url, params=params)).json()["id"]
    other_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(User(id=other_id, tenant_id=d.tenant, username=other_id.hex, email=other_id.hex + "@fixture.test",
            password_hash="fixture", display_name="Other reader", role="member"))
        db.add(AgentPermission(tenant_id=d.tenant, agent_id=d.agent, scope_type="user", scope_id=other_id, access_level="use"))
        await db.commit()
    headers = {"Authorization": "Bearer " + create_access_token(str(other_id), "member", str(d.tenant))}
    assert (await client.get(url, params=params, headers=headers)).status_code == 403
    assert (await client.get(f"/chat/attachments/{ref}/download", headers=headers)).status_code == 403
    async with owner_sessionmaker() as db:
        grant = ResourcePermission(id=uuid.uuid4(), tenant_id=d.tenant, resource_type="workspace_file",
            resource_id=workspace_resource_id(d.agent, path), principal_type="user", principal_id=other_id,
            actions=["read"], effect="allow", conditions={})
        db.add(grant)
        await db.commit()
    assert (await client.get(url, params=params, headers=headers)).status_code == 200
    downloaded = await client.get(f"/chat/attachments/{ref}/download", headers=headers)
    assert downloaded.status_code == 200 and downloaded.content == BODY
    async with owner_sessionmaker() as db:
        await db.delete(await db.get(ResourcePermission, grant.id))
        await db.commit()
    requests = len(provider[1].requests)
    assert (await client.get(f"/chat/attachments/{ref}/download", headers=headers)).status_code == 403
    assert len(provider[1].requests) == requests
    assert (await client.get(url, params={**params, "path": "workspace/report.txt"})).status_code == 404


async def test_failed_channel_work_copy_keeps_original_intent_for_safe_retry(
    original_api, provider, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.models.storage_blob import StorageBlob, StorageBlobRef
    from app.services import feishu_uploads

    d, client = original_api
    with monkeypatch.context() as failure:
        def fail(*args):
            raise OSError("Synthetic channel work-copy failure")
        failure.setattr(feishu_uploads, "_store_upload", fail)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, d.tenant)
            with pytest.raises(OSError, match="Synthetic channel"):
                await save(db, d, "feishu")
            await db.rollback()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        blobs = (await db.scalars(select(StorageBlob).where(StorageBlob.tenant_id == d.tenant, StorageBlob.kind == "chat_upload"))).all()
        assert len(blobs) == 1 and blobs[0].state == "uploading"
        assert (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.blob_id == blobs[0].id))).all() == []
        path = await save(db, d, "feishu")
        await db.commit()
    ref = (await client.get("/chat/attachments/workspace-original", params={"agent_id": str(d.agent), "path": path})).json()["id"]
    downloaded = await client.get(f"/chat/attachments/{ref}/download")
    assert downloaded.status_code == 200 and downloaded.content == BODY
    if provider[0] == "s3":
        assert provider[1].writes == 1


@pytest.mark.parametrize("condition", ["missing", "corrupt", "rebound"])
async def test_channel_original_never_substitutes_current_or_rebound_source_bytes(
    original_api, provider, app_user_sessionmaker, condition,  # noqa: F811
):
    from app.models.storage_blob import StorageBlob, StorageBlobRef
    from app.models.workspace_resource import WorkspaceResourceManifest

    d, client = original_api
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        path = await save(db, d, "feishu")
        await db.commit()
    found = (await client.get("/chat/attachments/workspace-original", params={"agent_id": str(d.agent), "path": path})).json()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        ref = await db.get(StorageBlobRef, uuid.UUID(found["id"]))
        blob = await db.get(StorageBlob, ref.blob_id)
        if condition == "rebound":
            manifest = await db.get(WorkspaceResourceManifest, uuid.UUID(ref.owner_id))
            manifest.root_session_id = None
            await db.commit()
        elif provider[0] == "s3":
            if condition == "missing":
                provider[1].objects.clear()
            else:
                provider[1].mode = "corrupt"
        else:
            target = d.root.parent / ".storage_objects" / blob.bucket / blob.object_key
            if condition == "missing":
                target.unlink()
            else:
                target.write_bytes(b"CORRUPT")
    (d.root / path).write_bytes(b"CURRENT_IS_NOT_THE_ORIGINAL")
    requests = len(provider[1].requests)
    response = await client.get(f"/chat/attachments/{found['id']}/download")
    assert response.status_code == 503 and response.json()["detail"]["code"] == "original_attachment_unavailable"
    assert b"CURRENT_IS_NOT_THE_ORIGINAL" not in response.content and b"CHANNEL_ORIGINAL" not in response.content
    if condition == "rebound":
        assert len(provider[1].requests) == requests


async def test_mounted_file_card_consumes_native_original_api(original_api, provider, app_user_sessionmaker, tmp_path_factory):  # noqa: F811
    from app.config import get_settings

    d, client = original_api
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        path = await save(db, d, "feishu")
        await db.commit()
    (d.root / path).write_bytes(b"CURRENT_WORK_COPY")
    root = Path(__file__).resolve().parents[3]
    work = tmp_path_factory.mktemp("channel-original-component")
    (work / "original_runtime.py").write_text("from fastapi import FastAPI\nfrom app.api.upload import router\n"
        "from app.core.tenant_middleware import TenantMiddleware\napp=FastAPI()\n"
        "app.add_middleware(TenantMiddleware)\napp.include_router(router,prefix='/api')\n")
    settings = get_settings()
    values = {}
    for key in ("SECRET_KEY", "JWT_SECRET_KEY", "BLOB_STORAGE_PROVIDER", "BLOB_S3_ENDPOINT", "BLOB_S3_REGION",
        "BLOB_S3_BUCKET", "BLOB_S3_ACCESS_KEY_ID", "BLOB_S3_SECRET_ACCESS_KEY", "BLOB_S3_SESSION_TOKEN"):
        value = getattr(settings, key)
        values[key] = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    environment = {**os.environ, **values, "DATABASE_URL": app_user_sessionmaker.kw["bind"].url.render_as_string(hide_password=False),
        "AGENT_DATA_DIR": str(d.root.parent), "PYTHONPATH": str(work) + os.pathsep + os.environ.get("PYTHONPATH", str(root / "backend"))}
    process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m", "uvicorn", "original_runtime:app", "--host", "127.0.0.1",
        "--port", str(port), "--no-access-log", env=environment, cwd=root / "backend",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    lines = []
    try:
        async def started():
            while line := await process.stdout.readline():
                lines.append(line)
                if b"Uvicorn running on" in line:
                    return
            pytest.fail("Native original API exited before its startup notification")
        await asyncio.wait_for(started(), 20)
        profile, output = work / "bridge.json", work / "bridge-result.json"
        profile.write_text(json.dumps({"origin": f"http://127.0.0.1:{port}", "token": client.headers["Authorization"].removeprefix("Bearer "),
            "actor": str(d.user.id), "tenant": str(d.tenant), "agent": str(d.agent), "path": path,
            "digest": hashlib.sha256(BODY).hexdigest(), "output": str(output)}))
        profile.chmod(0o600)
        with (work / "frontend.log").open("wb") as log:
            node = await asyncio.create_subprocess_exec(str(root / "frontend/node_modules/.bin/vitest"), "run",
                "src/pages/agent-detail/ChannelOriginal.native.test.tsx", cwd=root / "frontend",
                env={**os.environ, "HIVE_CHANNEL_ORIGINAL_BRIDGE": str(profile), "NODE_OPTIONS": "--no-experimental-webstorage"},
                stdout=log, stderr=asyncio.subprocess.STDOUT)
            try:
                code = await asyncio.wait_for(node.wait(), 90)
            finally:
                if node.returncode is None:
                    node.terminate()
                    await asyncio.wait_for(node.wait(), 10)
        assert code == 0, (work / "frontend.log").read_text()[-4000:]
        receipt = json.loads(output.read_text())
        assert receipt["digest"] == hashlib.sha256(BODY).hexdigest() and receipt["size"] == len(BODY)
        assert (d.root / path).read_bytes() == b"CURRENT_WORK_COPY"
    finally:
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 10)
        lines.append(await process.stdout.read())
        (work / "server.log").write_bytes(b"".join(lines))
