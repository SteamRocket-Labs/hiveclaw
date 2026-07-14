from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _docx_bytes(text: str) -> bytes:
    from docx import Document

    buffer = BytesIO()
    document = Document()
    document.add_paragraph(text)
    document.save(buffer)
    return buffer.getvalue()


class _PreviewAdapter:
    def version(self):
        return "1.0.88"

    def run_view(self, _path, *, mode, page=None, cwd=None):
        assert mode == "html"
        assert page is None
        assert cwd is not None
        return {
            "success": True,
            "data": "<!DOCTYPE html><html><head></head><body>Agent updated document</body></html>",
        }


@pytest.mark.asyncio
async def test_office_create_agent_update_preview_and_readback_loop(tmp_path, monkeypatch):
    import app.api.office as office_api
    from app.services.agent_tool_domains.workspace import _read_document
    from app.services.office_document_service import OfficeDocumentService

    agent_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id, name="Rocky")
    monkeypatch.setattr(
        office_api,
        "settings",
        SimpleNamespace(AGENT_DATA_DIR=str(tmp_path), OFFICECLI_PREVIEW_MAX_BYTES=1024 * 1024),
    )

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

    monkeypatch.setattr(office_api, "check_agent_access", fake_access)
    monkeypatch.setattr(office_api, "authorize_workspace_path", fake_authorize)
    monkeypatch.setattr(office_api, "register_workspace_path", fake_register)

    created = await office_api.create_office_document(
        agent_id=agent_id,
        body=office_api.OfficeDocumentCreateIn(path="workspace/demo.docx", kind="docx"),
        current_user=user,
        db=SimpleNamespace(),
    )
    assert created["status"] == "ok"

    workspace = tmp_path / str(agent_id)
    service = OfficeDocumentService(workspace, adapter=_PreviewAdapter(), preview_max_bytes=1024 * 1024)
    service.atomic_save_bytes(
        "workspace/demo.docx",
        _docx_bytes("Agent updated document"),
        reason="agent-office-update",
    )
    rendered = service.render_preview("workspace/demo.docx")
    monkeypatch.setattr(office_api.OfficeDocumentService, "render_preview", lambda *_args, **_kwargs: rendered)

    response = await office_api.preview_office_document(
        agent_id=agent_id,
        path="workspace/demo.docx",
        operator_view=False,
        operator_reason=None,
        current_user=user,
        db=SimpleNamespace(),
    )

    assert response.headers["x-office-preview-mode"] == "html"
    assert response.headers["x-office-renderer-version"] == "1.0.88"
    assert b"Agent updated document" in response.body
    readback = await _read_document(workspace, "workspace/demo.docx")
    assert "Agent updated document" in readback
    manifest = service.manifest_path("workspace/demo.docx").read_text(encoding="utf-8")
    assert "agent-office-update" in manifest
    assert "active_editor_session" not in manifest
