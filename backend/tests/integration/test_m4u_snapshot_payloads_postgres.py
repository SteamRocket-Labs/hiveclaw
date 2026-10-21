"""Shared physical bytes retain independent authenticated delivery references."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import delete, text

from app.api import files
from app.database import pin_rls_tenant_context
from tests.integration.conftest import app_user_engine, migrated_pg_url as _migrated_pg_url, pg_container as _pg_container  # noqa: F401
from tests.integration.test_m1g22_artifact_versions_postgres import delivered  # noqa: F401

REAL_DOWNLOAD_IDENTITY = files._load_download_user_from_jwt
ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
def pg_container():
    # Downgrade checks own their catalog and must not mutate another test's DB.
    yield from _pg_container.__wrapped__()


@pytest.fixture
def migrated_pg_url(pg_container):
    return _migrated_pg_url.__wrapped__(pg_container)


async def test_shared_bytes_use_real_jwt_rls_and_live_run_retention(
    delivered, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.core.security import create_access_token, decode_access_token
    from app.database import get_db
    from app.models.agent import AgentPermission
    from app.models.audit import ChatMessage
    from app.models.chat_artifact import ChatArtifact
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.services.chat_artifact_delivery import create_chat_artifacts_for_message, cleanup_chat_artifact_snapshots_for_agent
    from app.services.content_input_receipts import record_workspace_receipt
    from app.services.workspace_resource_authority import register_workspace_path

    d = delivered
    second_path = d.root / "workspace/other.txt"
    second_path.write_bytes(b"AAAA")
    other = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(User(id=other, tenant_id=d.tenant, username=str(other), email=f"{other}@fixture.test",
            display_name="Other member", password_hash="fixture", role="member", is_active=True))
        db.add(AgentPermission(tenant_id=d.tenant, agent_id=d.agent, scope_type="user", scope_id=other, access_level="use"))
        await register_workspace_path(db, tenant_id=d.tenant, agent_id=d.agent, path="workspace/other.txt",
            owner_user_id=d.user.id, root_session_id=d.session, source="fixture", content_hash=hashlib.sha256(b"AAAA").hexdigest())
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        assert tuple((await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()) == (False, False)
        record_workspace_receipt(db, d.tenant, d.agent, "workspace/other.txt", hashlib.sha256(b"AAAA").hexdigest(), None)
        message = ChatMessage(tenant_id=d.tenant, agent_id=d.agent, user_id=d.user.id,
            role="assistant", content="Delivered second source", conversation_id=str(d.session))
        db.add(message)
        await db.flush()
        parts = await create_chat_artifacts_for_message(db=db, tenant_id=d.tenant, agent_id=d.agent,
            session_id=d.session, runtime_task_id=d.run, message_id=message.id,
            paths=["workspace/other.txt"], workspace_root=d.root)
        second = await db.get(ChatArtifact, uuid.UUID(parts[0]["artifact_id"]))
        await db.commit()
    assert d.first.id != second.id and d.first.snapshot_hash != second.snapshot_hash
    stored = d.root / d.first.snapshot_json["snapshot_storage_path"]
    assert stored == d.root / second.snapshot_json["snapshot_storage_path"]
    assert len(list(stored.parent.iterdir())) == 1 and stored.stat().st_nlink == 1
    assert len({path.stat().st_ino for path in (stored, d.target, second_path)}) == 3
    d.target.write_bytes(b"EDITED")
    second_path.write_bytes(b"OTHER_EDIT")
    monkeypatch.setattr(files, "_load_download_user_from_jwt", REAL_DOWNLOAD_IDENTITY)

    async def scoped_db(request: Request):
        token = request.headers["authorization"].removeprefix("Bearer ")
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, uuid.UUID(decode_access_token(token)["tid"]))
            yield db
            await db.commit()

    app = FastAPI()
    app.include_router(files.router)
    app.dependency_overrides[get_db] = scoped_db
    headers = {"Authorization": "Bearer " + create_access_token(str(d.user.id), "member", str(d.tenant))}
    other_headers = {"Authorization": "Bearer " + create_access_token(str(other), "member", str(d.tenant))}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://snapshot.test", headers=headers) as client:
        for artifact in (d.first, second):
            url = f"/agents/{d.agent}/files/artifacts/{artifact.id}/download"
            result = await client.get(url)
            assert result.status_code == 200 and result.content == b"AAAA", result.text
            denied = await client.get(url, headers=other_headers)
            assert denied.status_code == 403 and b"AAAA" not in denied.content, denied.text
        for suffix in ("content", "download"):
            denied = await client.get(f"/agents/{d.agent}/files/{suffix}", params={"path": d.first.snapshot_json["snapshot_storage_path"]})
            assert denied.status_code == 403, denied.text
    for path in (stored, *(d.root / row.snapshot_json["snapshot_ref_path"] for row in (d.first, second))):
        os.utime(path, (1, 1))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        await db.execute(delete(ChatArtifact).where(ChatArtifact.agent_id == d.agent))
        await db.commit()
        await pin_rls_tenant_context(db, d.tenant)
        held = await cleanup_chat_artifact_snapshots_for_agent(db=db, agent_id=d.agent, workspace_root=d.root, retention_days=0)
        await db.commit()
        assert stored.read_bytes() == b"AAAA" and held["kept_active_count"] == 1
        await pin_rls_tenant_context(db, d.tenant)
        (await db.get(RuntimeTask, d.run)).status = "completed"
        await db.commit()
        await pin_rls_tenant_context(db, d.tenant)
        removed = await cleanup_chat_artifact_snapshots_for_agent(db=db, agent_id=d.agent, workspace_root=d.root, retention_days=0)
        await db.commit()
    assert not stored.exists() and removed["removed_payload_count"] == 1
    assert removed["removed_count"] == 3
    assert d.target.read_bytes() == b"EDITED" and second_path.read_bytes() == b"OTHER_EDIT"


async def test_format_downgrade_preserves_shared_artifacts_and_server_references(delivered, owner_sessionmaker):  # noqa: F811
    import app.models.session_v2  # noqa: F401
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.services.chat_snapshot_store import PAYLOAD_SCHEMA

    d = delivered
    owner_url = owner_sessionmaker.kw["bind"].url.render_as_string(hide_password=False)
    marker = {"type": "artifact", "snapshot_payload_schema": PAYLOAD_SCHEMA, "artifact_id": str(d.first.id)}

    def downgrade():
        return subprocess.run([sys.executable, "-B", "-m", "alembic", "downgrade", "explicit_memory_sources_1021"],
            cwd=ROOT / "backend", env={**os.environ, "DATABASE_URL": owner_url}, capture_output=True, timeout=30)

    async def refused():
        result = downgrade()
        assert result.returncode != 0 and b"chat_snapshot_payloads_require_recovery_before_downgrade" in result.stderr
        async with owner_sessionmaker() as db:
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "chat_snapshot_payload_refs_1021"

    await refused()
    async with owner_sessionmaker() as db:
        # Separately prove that references outliving their artifact row are guarded.
        await db.delete(await db.get(ChatArtifact, d.first.id))
        event = ChatTranscriptEvent(tenant_id=d.tenant, agent_id=d.agent, session_id=d.session, sequence=100,
            actor_type="agent", event_type="assistant_message", parts_json=[marker])
        db.add(event)
        await db.commit()
        event_id = event.id
    await refused()
    async with owner_sessionmaker() as db:
        event = await db.get(ChatTranscriptEvent, event_id)
        event.parts_json, event.metadata_json = None, {"nested": [marker]}
        await db.commit()
    await refused()
    async with owner_sessionmaker() as db:
        # User-supplied protocol-looking data does not become a server format obligation.
        (await db.get(ChatTranscriptEvent, event_id)).actor_type = "user"
        (await db.get(RuntimeTask, d.run)).metadata_json = {"output": marker}
        await db.commit()
    await refused()
    async with owner_sessionmaker() as db:
        (await db.get(RuntimeTask, d.run)).metadata_json = {"text": json.dumps(marker)}
        await db.commit()
    assert downgrade().returncode == 0
    upgraded = subprocess.run([sys.executable, "-B", "-m", "alembic", "upgrade", "head"], cwd=ROOT / "backend",
        env={**os.environ, "DATABASE_URL": owner_url}, capture_output=True, timeout=30)
    assert upgraded.returncode == 0, upgraded.stderr.decode()
    assert (d.root / d.first.snapshot_json["snapshot_storage_path"]).read_bytes() == b"AAAA"
