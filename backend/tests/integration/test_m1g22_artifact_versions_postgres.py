"""Exact delivered versions survive reopening without raw snapshot access."""

import hashlib
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.chat_artifact import ChatArtifact
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def delivered(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    from app.api import files
    from app.config import get_settings
    from app.core.security import get_current_user
    from app.database import get_db
    from app.models.audit import ChatMessage
    from app.models.user import User
    from app.services.chat_artifact_delivery import create_chat_artifacts_for_message
    from app.services.content_input_receipts import latest_workspace_receipt, record_workspace_receipt
    from app.services.workspace_resource_authority import register_workspace_path

    tenant, user_id, agent, session, run = await _seed_session(owner_sessionmaker, active_run=True)
    root = tmp_path / str(agent)
    target = root / "workspace/report.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"AAAA")
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        user.role = "member"
        await register_workspace_path(db, tenant_id=tenant, agent_id=agent, path="workspace/report.txt",
            owner_user_id=user_id, root_session_id=session, source="fixture",
            content_hash=hashlib.sha256(target.read_bytes()).hexdigest())
        await db.commit()

    async def publish():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            # These exact fixture originals have known authorship before snapshot capture.
            body = target.read_bytes()
            assert body in {b"AAAA", b"BBBB"}
            record_workspace_receipt(db, tenant, agent, "workspace/report.txt", hashlib.sha256(body).hexdigest(),
                await latest_workspace_receipt(db, tenant, agent, "workspace/report.txt"))
            await db.flush()
            message = ChatMessage(id=uuid.uuid4(), tenant_id=tenant, agent_id=agent, user_id=user_id,
                role="assistant", content="Delivered", conversation_id=str(session))
            db.add(message)
            await db.flush()
            parts = await create_chat_artifacts_for_message(db=db, tenant_id=tenant, agent_id=agent,
                session_id=session, runtime_task_id=run, message_id=message.id,
                paths=["workspace/report.txt"], workspace_root=root)
            artifact = await db.get(ChatArtifact, uuid.UUID(parts[0]["artifact_id"]))
            await db.commit()
            return artifact

    async def current_user():
        return user

    async def identity(**_kwargs):
        return user

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            yield db
            await db.commit()

    monkeypatch.setattr(files, "_load_download_user_from_jwt", identity)
    app = FastAPI()
    app.include_router(files.router)
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = scoped_db
    first = await publish()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local",
        headers={"Authorization": "Bearer fixture-identity"}) as client:
        yield SimpleNamespace(tenant=tenant, user=user, agent=agent, session=session, run=run,
            root=root, target=target, first=first, publish=publish, client=client,
            url=f"/agents/{agent}/files/artifacts/{first.id}")


async def test_reopen_download_and_a2a_consume_distinct_same_stat_versions(delivered, app_user_sessionmaker):
    from app.runtime.a2a_workflow import A2AOutputArtifact
    from app.services.a2a_workflow_artifacts import read_verified_artifact

    d = delivered
    stat = d.target.stat()
    d.target.write_bytes(b"BBBB")
    os.utime(d.target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    second = await d.publish()
    assert second.id != d.first.id and second.snapshot_hash != d.first.snapshot_hash
    for artifact, expected in ((d.first, b"AAAA"), (second, b"BBBB")):
        url = f"/agents/{d.agent}/files/artifacts/{artifact.id}"
        content = await d.client.get(url + "/content")
        assert content.status_code == 200, content.text
        assert content.json()["content"].encode() == expected
        assert content.json()["content_hash"] == hashlib.sha256(expected).hexdigest()
        assert content.json()["workspace_changed"] is (artifact.id == d.first.id)
        download = await d.client.get(url + "/download")
        assert download.status_code == 200 and download.content == expected
        assert "report.txt" in download.headers["content-disposition"]
        text, digest = read_verified_artifact(artifact, d.root, tenant_id=d.tenant,
            requester_user_id=d.user.id, producer_agent_id=d.agent, producer_session_id=d.session,
            producer_runtime_task_id=d.run, output=A2AOutputArtifact(name="report", path=artifact.path), max_bytes=100)
        assert text.encode() == expected and digest == hashlib.sha256(expected).hexdigest()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        rows = (await db.scalars(select(ChatArtifact).where(ChatArtifact.agent_id == d.agent))).all()
        assert len(rows) == 2
        assert all(row.owner_user_id == d.user.id and row.root_session_id == d.session for row in rows)
        assert all(row.authority_state == "owned" for row in rows)
    assert d.target.read_bytes() == b"BBBB"


async def test_rejected_existing_snapshot_does_not_move_its_message(delivered, app_user_sessionmaker):
    from app.models.audit import ChatMessage
    from app.services.chat_artifact_delivery import create_chat_artifacts_for_message

    d = delivered
    original_message_id = d.first.message_id
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        existing = await db.get(ChatArtifact, d.first.id)
        existing.snapshot_json = {key: value for key, value in existing.snapshot_json.items() if key != "content_input_receipt_id"}
        message = ChatMessage(tenant_id=d.tenant, agent_id=d.agent, user_id=d.user.id,
            role="assistant", content="Reopen the existing report", conversation_id=str(d.session))
        db.add(message)
        await db.flush()
        errors = []
        parts = await create_chat_artifacts_for_message(db=db, tenant_id=d.tenant, agent_id=d.agent,
            session_id=d.session, runtime_task_id=d.run, message_id=message.id,
            paths=["workspace/report.txt"], workspace_root=d.root, rebind_existing_to_message=True, publication_errors=errors)
        assert parts == [] and errors[0]["reason_code"] == "workspace_content_unverified", (parts, errors)
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        assert (await db.get(ChatArtifact, d.first.id)).message_id == original_message_id


async def test_corrupt_snapshot_is_neither_reopened_nor_downloaded(delivered):
    d = delivered
    stored = d.root / d.first.snapshot_json["snapshot_storage_path"]
    stored.write_bytes(b"UNTRUSTED_CACHE_BYTES")
    for suffix in ("/content", "/download"):
        response = await d.client.get(d.url + suffix)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "workspace_resource_changed"
        assert "UNTRUSTED_CACHE_BYTES" not in response.text
    assert d.target.read_bytes() == b"AAAA"


@pytest.mark.parametrize("alias", ["leaf", "parent", "hardlink"])
async def test_snapshot_aliases_are_denied_even_when_the_declared_digest_matches(delivered, alias):
    d = delivered
    stored = d.root / d.first.snapshot_json["snapshot_storage_path"]
    private = d.root / "private" / stored.name
    private.parent.mkdir()
    private.write_bytes(b"AAAA")
    stored.unlink()
    if alias == "parent":
        stored.parent.rmdir()
        stored.parent.symlink_to(private.parent, target_is_directory=True)
    elif alias == "hardlink":
        os.link(private, stored)
    else:
        stored.symlink_to(private)
    for suffix in ("/content", "/download"):
        response = await d.client.get(d.url + suffix)
        assert response.status_code in {403, 409}, response.text
        assert "AAAA" not in response.text
    assert private.read_bytes() == b"AAAA"


async def test_download_pins_bytes_until_asgi_completion_and_cleans_private_copy(delivered, monkeypatch):
    from app.api.files import _WorkspaceDownloadResponse

    d = delivered
    original = _WorkspaceDownloadResponse.__call__
    seen = []

    async def replace_after_response_creation(self, scope, receive, send):
        seen.append(Path(self.path))
        (d.root / d.first.snapshot_json["snapshot_storage_path"]).write_bytes(b"AFTER_CAPTURE")
        await original(self, scope, receive, send)

    monkeypatch.setattr(_WorkspaceDownloadResponse, "__call__", replace_after_response_creation)
    response = await d.client.get(d.url + "/download")
    assert response.status_code == 200 and response.content == b"AAAA"
    assert len(seen) == 1 and not seen[0].exists()


@pytest.mark.parametrize("legacy", [False, True])
async def test_internal_snapshot_paths_require_artifact_authority_for_http_and_native_tools(delivered, legacy):
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope
    from app.tools.adapters import adapt_and_call
    from app.tools.handlers import filesystem
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    d = delivered
    path = ".chat_artifact_snapshots/old/report.txt" if legacy else d.first.snapshot_json["snapshot_storage_path"]
    target = d.root / path
    if legacy:
        target.parent.mkdir(parents=True)
        target.write_bytes(b"LEGACY_SNAPSHOT")
    original = target.read_bytes()
    for raw_path in (path, "./" + path):
        for suffix in ("content", "download"):
            response = await d.client.get(f"/agents/{d.agent}/files/{suffix}", params={"path": raw_path})
            assert response.status_code == 403, response.text
        response = await d.client.put(f"/agents/{d.agent}/files/content", params={"path": raw_path}, json={"content": "overwrite"})
        assert response.status_code == 403, response.text
        for scope in (None, WorkspaceAuthorityScope(agent_id=d.agent, user_id=d.user.id,
            root_session_id=d.session, allowed_paths=frozenset(), operator_view=True, authority_source="fixture")):
            context = ToolExecutionContext(agent_id=d.agent, user_id=d.user.id, tenant_id=str(d.tenant),
                workspace=d.root, session_id=str(d.session), workspace_authority_scope=scope)
            for tool in (filesystem.read_file, filesystem.write_file):
                result = await adapt_and_call(tool.meta, tool, ToolExecutionRequest(tool_name=tool.meta.name,
                    arguments={"path": raw_path, "content": "overwrite"}, context=context))
                assert "auth_or_permission" in str(result), result
                assert original.decode() not in str(result)
    assert target.read_bytes() == original
    assert (await d.client.get(d.url + "/content")).status_code == 200
    assert (await d.client.get(f"/agents/{d.agent}/files/content", params={"path": "workspace/report.txt"})).status_code == 200
