from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.api.local_bridge as local_bridge_api
from app.database import get_db
from app.services.local_bridge_service import BridgeAuthContext


def test_bridge_upload_uses_bound_context_identity(monkeypatch) -> None:
    connection_id = uuid4()
    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    context = BridgeAuthContext(
        connection_id=connection_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        scopes=("files:upload",),
        client_kind="generic_mcp_stdio",
        device_name="Workstation",
    )
    captured = {}

    async def fake_require_policy(db, *, tenant_id, agent_id, capability):
        captured["policy"] = {
            "db": db,
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "capability": capability,
        }

    async def fake_save_bridge_upload(*, file, context, db):
        captured["filename"] = file.filename
        captured["context"] = context
        return {
            "filename": file.filename,
            "workspace_path": "workspace/uploads/report.md",
            "artifacts": [{"path": "workspace/uploads/report.md"}],
        }

    app = FastAPI()
    app.include_router(local_bridge_api.router)

    async def override_db():
        yield object()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[local_bridge_api.get_bridge_auth_context] = lambda: context
    monkeypatch.setattr(local_bridge_api, "save_bridge_upload", fake_save_bridge_upload)
    monkeypatch.setattr(
        local_bridge_api.bridge_service,
        "require_local_agent_capability_policy",
        fake_require_policy,
        raising=False,
    )
    client = TestClient(app)

    resp = client.post(
        "/local-bridge/upload",
        headers={"Authorization": "Bearer hb_secret"},
        files={"file": ("report.md", b"# bridge upload\n", "text/markdown")},
    )

    assert resp.status_code == 200
    assert resp.json()["workspace_path"] == "workspace/uploads/report.md"
    assert captured["filename"] == "report.md"
    assert captured["context"] is context
    assert captured["policy"] == {
        "db": captured["policy"]["db"],
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "capability": "local_agent.file_upload",
    }


def test_unbound_bridge_upload_fails_closed_before_workspace_write(monkeypatch, tmp_path) -> None:
    import app.api.upload as upload_api

    tenant_id = uuid4()
    user_id = uuid4()
    context = BridgeAuthContext(
        connection_id=uuid4(),
        tenant_id=tenant_id,
        agent_id=None,
        user_id=user_id,
        scopes=("files:upload",),
        client_kind="codex",
        device_name="Codex local runner",
    )
    monkeypatch.setattr(upload_api, "WORKSPACE_ROOT", tmp_path)

    app = FastAPI()
    app.include_router(local_bridge_api.router)

    async def override_db():
        yield object()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[local_bridge_api.get_bridge_auth_context] = lambda: context

    async def fake_require_policy(_db, *, tenant_id, agent_id, capability):
        assert tenant_id == context.tenant_id
        assert agent_id is None
        assert capability == "local_agent.file_upload"
        raise HTTPException(status_code=409, detail="Local bridge connection is not bound to an Agent")

    monkeypatch.setattr(
        local_bridge_api.bridge_service,
        "require_local_agent_capability_policy",
        fake_require_policy,
        raising=False,
    )
    client = TestClient(app)

    resp = client.post(
        "/local-bridge/upload",
        headers={"Authorization": "Bearer hb_secret"},
        files={"file": ("report.md", b"# user scoped upload\n", "text/markdown")},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Local bridge connection is not bound to an Agent"
    saved_path = (
        tmp_path / "local_agents" / str(tenant_id) / "users" / str(user_id) / "workspace" / "uploads" / "report.md"
    )
    assert not saved_path.exists()


def test_bridge_upload_live_policy_deny_prevents_storage(monkeypatch) -> None:
    context = BridgeAuthContext(
        connection_id=uuid4(),
        tenant_id=uuid4(),
        agent_id=uuid4(),
        user_id=uuid4(),
        scopes=("files:upload",),
        client_kind="hive-connect",
        device_name="Owner Mac",
    )
    saved = False

    async def fake_require_policy(_db, *, tenant_id, agent_id, capability):
        assert tenant_id == context.tenant_id
        assert agent_id == context.agent_id
        assert capability == "local_agent.file_upload"
        raise HTTPException(status_code=403, detail="Local Agent capability denied by live policy")

    async def fake_save_bridge_upload(*, file, context, db):
        nonlocal saved
        saved = True
        return {}

    app = FastAPI()
    app.include_router(local_bridge_api.router)

    async def override_db():
        yield object()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[local_bridge_api.get_bridge_auth_context] = lambda: context
    monkeypatch.setattr(
        local_bridge_api.bridge_service,
        "require_local_agent_capability_policy",
        fake_require_policy,
        raising=False,
    )
    monkeypatch.setattr(local_bridge_api, "save_bridge_upload", fake_save_bridge_upload)
    client = TestClient(app)

    response = client.post(
        "/local-bridge/upload",
        files={"file": ("blocked.md", b"must not persist", "text/markdown")},
    )

    assert response.status_code == 403
    assert saved is False
