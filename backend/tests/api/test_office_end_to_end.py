from __future__ import annotations

from contextlib import asynccontextmanager
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _settings(tmp_path, *, secret: str = "onlyoffice-secret"):
    return SimpleNamespace(
        AGENT_DATA_DIR=str(tmp_path),
        BASE_URL="https://hive.example.com",
        PUBLIC_BASE_URL="https://hive.example.com",
        ONLYOFFICE_DOCS_URL="https://docs.example.com",
        ONLYOFFICE_INTERNAL_DOCS_URL="https://docs.internal",
        ONLYOFFICE_JWT_SECRET=secret,
        ONLYOFFICE_DOWNLOAD_TOKEN_EXPIRE_SECONDS=300,
        JWT_SECRET_KEY="jwt-secret",
        JWT_ALGORITHM="HS256",
    )


def _docx_bytes(text: str) -> bytes:
    from docx import Document

    buffer = BytesIO()
    document = Document()
    document.add_paragraph(text)
    document.save(buffer)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_office_docx_create_edit_callback_and_readback_loop(tmp_path, monkeypatch):
    import app.api.office as office_api
    from app.services.agent_tool_domains.workspace import _read_document
    from app.services.office_document_service import OfficeDocumentService
    from app.tools.handlers.office import office_document_apply

    monkeypatch.setattr(office_api, "settings", _settings(tmp_path))

    agent_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()
    user = SimpleNamespace(id=user_id, name="Rocky")

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "use"

    async def fake_authorize(_db, _user, **_kwargs):
        return SimpleNamespace(
            owner_user_id=user_id,
            root_session_id=None,
            authority_source="resource_owner",
            operator_view=False,
            manifest=SimpleNamespace(),
        )

    async def fake_register(*_args, **_kwargs):
        return None

    @asynccontextmanager
    async def fake_token_authority(*, agent_id, path, payload, expected_action):
        assert payload["authority_action"] == expected_action
        yield {
            "db": SimpleNamespace(),
            "user": user,
            "agent": SimpleNamespace(id=agent_id, tenant_id=tenant_id),
            "decision": SimpleNamespace(owner_user_id=user_id, root_session_id=None),
            "target": tmp_path / str(agent_id) / path,
            "path": path,
        }

    monkeypatch.setattr(office_api, "check_agent_access", fake_access)
    monkeypatch.setattr(office_api, "authorize_workspace_path", fake_authorize)
    monkeypatch.setattr(office_api, "register_workspace_path", fake_register)
    monkeypatch.setattr(office_api, "_authorize_office_token_payload", fake_token_authority)

    created = await office_api.create_office_document(
        agent_id=agent_id,
        body=office_api.OfficeDocumentCreateIn(path="workspace/demo.docx", kind="docx"),
        current_user=user,
        db=SimpleNamespace(),
    )
    assert created["status"] == "ok"

    config = await office_api.get_editor_config(
        agent_id=agent_id,
        path="workspace/demo.docx",
        mode="edit",
        current_user=user,
        db=SimpleNamespace(),
    )
    assert config["enabled"] is True

    workspace = tmp_path / str(agent_id)
    protected_write = await office_document_apply(
        workspace,
        {"path": "workspace/demo.docx", "operations": [{"op": "replace_text", "from": "old", "to": "new"}]},
    )
    assert '"ok": false' in protected_write
    assert "active_editor_session" in protected_write

    class _Response:
        content = _docx_bytes("Saved through ONLYOFFICE callback")

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            assert url == "https://docs.internal/download/demo.docx"
            return _Response()

    monkeypatch.setattr(office_api.httpx, "AsyncClient", lambda *args, **kwargs: _Client())
    callback_token = office_api.make_document_token(
        agent_id=agent_id,
        path="workspace/demo.docx",
        purpose="callback",
        user_id=user_id,
    )
    callback_result = await office_api.onlyoffice_callback(
        agent_id=agent_id,
        path="workspace/demo.docx",
        token=callback_token,
        payload=office_api.OnlyOfficeCallback(status=2, url="https://docs.example.com/download/demo.docx"),
    )

    assert callback_result == {"error": 0}
    readback = await _read_document(workspace, "workspace/demo.docx")
    assert "Saved through ONLYOFFICE callback" in readback

    service = OfficeDocumentService(workspace)
    manifest = service.manifest_path("workspace/demo.docx").read_text(encoding="utf-8")
    assert "onlyoffice-status-2" in manifest
    assert service.get_active_editor_session("workspace/demo.docx") is not None

    await office_api.onlyoffice_callback(
        agent_id=agent_id,
        path="workspace/demo.docx",
        token=callback_token,
        payload=office_api.OnlyOfficeCallback(status=4),
    )
    assert service.get_active_editor_session("workspace/demo.docx") is None
