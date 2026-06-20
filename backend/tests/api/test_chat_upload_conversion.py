from __future__ import annotations

import io
import sys
import uuid
from types import SimpleNamespace

import pytest
from fastapi import UploadFile


def _install_fake_markitdown(monkeypatch, output: str) -> None:
    class _FakeMarkItDown:
        def convert_local(self, _source: str):
            return SimpleNamespace(text_content=output)

    monkeypatch.setitem(sys.modules, "markitdown", SimpleNamespace(MarkItDown=_FakeMarkItDown))


@pytest.mark.asyncio
async def test_chat_upload_routes_documents_through_conversion_service(monkeypatch, tmp_path):
    import app.api.upload as upload_api

    async def fake_check_agent_access(db, current_user, agent_id):
        return None

    _install_fake_markitdown(monkeypatch, "# Uploaded\n\nConverted markdown")
    monkeypatch.setattr(upload_api, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(upload_api, "check_agent_access", fake_check_agent_access)

    agent_id = uuid.uuid4()
    file = UploadFile(io.BytesIO(b"<h1>raw</h1>"), filename="report.html")

    result = await upload_api.upload_file(
        file=file,
        agent_id=agent_id,
        current_user=SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4()),
        db=object(),
    )

    assert result["workspace_path"] == "workspace/uploads/report.html"
    assert result["conversion"]["status"] == "converted"
    assert result["conversion"]["engine"] == "local_markitdown"
    assert result["conversion"]["markdown_path"].startswith("workspace/.hive/document_conversions/")
    assert result["preview_text"].startswith("Converted with local_markitdown.")
    assert "Full Markdown: workspace/.hive/document_conversions/" in result["extracted_text"]

    artifact = tmp_path / str(agent_id) / result["conversion"]["markdown_path"]
    assert artifact.read_text(encoding="utf-8") == "# Uploaded\n\nConverted markdown"
