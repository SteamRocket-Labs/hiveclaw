from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
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
