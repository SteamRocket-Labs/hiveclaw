from __future__ import annotations

import io
import sys
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile


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
        background_tasks=None,
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


@pytest.mark.asyncio
async def test_chat_upload_default_registers_personal_kb_candidate(monkeypatch, tmp_path):
    import app.api.upload as upload_api

    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    document_id = uuid.uuid4()
    job_id = uuid.uuid4()
    captured: list[dict] = []

    async def fake_check_agent_access(db, current_user, checked_agent_id):
        assert checked_agent_id == agent_id
        return SimpleNamespace(id=agent_id, owner_user_id=owner_id, tenant_id=tenant_id), "write"

    class _FakePersonalKnowledgeService:
        async def queue_source_bytes_import(self, session, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(
                document_id=document_id,
                job_id=job_id,
                source_sha256="a" * 64,
                artifact_hash="a" * 64,
                canonical_md_path="",
                segment_count=0,
                status="queued",
                warnings=[],
            )

    class _FakeDB:
        def __init__(self) -> None:
            self.commit_count = 0

        async def commit(self) -> None:
            self.commit_count += 1

    _install_fake_markitdown(monkeypatch, "# Notes\n\nConverted markdown")
    monkeypatch.setattr(upload_api, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(upload_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(upload_api, "PersonalKnowledgeService", lambda: _FakePersonalKnowledgeService(), raising=False)

    db = _FakeDB()
    file = UploadFile(io.BytesIO(b"# notes"), filename="notes.md")
    result = await upload_api.upload_file(
        background_tasks=None,
        file=file,
        agent_id=agent_id,
        skip_personal_kb=False,
        current_user=SimpleNamespace(id=user_id, tenant_id=tenant_id),
        db=db,
    )

    assert captured[0]["tenant_id"] == tenant_id
    assert captured[0]["owner_user_id"] == owner_id
    assert captured[0]["created_by_user_id"] == user_id
    assert captured[0]["source_kind"] == "chat_attachment"
    assert captured[0]["source_uri"] == f"agent:{agent_id}:workspace/uploads/notes.md"
    assert captured[0]["doc_metadata"]["origin"] == f"agent:{agent_id}"
    assert captured[0]["doc_metadata"]["workspace_path"] == "workspace/uploads/notes.md"
    assert db.commit_count == 1
    assert result["personal_kb_candidate"] == {
        "skipped": False,
        "document_id": str(document_id),
        "job_id": str(job_id),
        "status": "queued",
        "warnings": [],
        "origin": f"agent:{agent_id}",
    }


@pytest.mark.asyncio
async def test_chat_upload_skip_personal_kb_does_not_ingest(monkeypatch, tmp_path):
    import app.api.upload as upload_api

    async def fake_check_agent_access(db, current_user, agent_id):
        return SimpleNamespace(id=agent_id, owner_user_id=current_user.id, tenant_id=current_user.tenant_id), "write"

    class _FailingPersonalKnowledgeService:
        async def queue_source_bytes_import(self, *args, **kwargs):
            raise AssertionError("skip_personal_kb must not queue")

    _install_fake_markitdown(monkeypatch, "# Notes\n\nConverted markdown")
    monkeypatch.setattr(upload_api, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(upload_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(
        upload_api, "PersonalKnowledgeService", lambda: _FailingPersonalKnowledgeService(), raising=False
    )

    agent_id = uuid.uuid4()
    file = UploadFile(io.BytesIO(b"# notes"), filename="notes.md")
    result = await upload_api.upload_file(
        background_tasks=None,
        file=file,
        agent_id=agent_id,
        skip_personal_kb=True,
        current_user=SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4()),
        db=object(),
    )

    assert result["personal_kb_candidate"] == {"skipped": True, "reason": "user_skip"}


@pytest.mark.asyncio
async def test_chat_upload_rejects_oversized_file_before_workspace_or_personal_kb(monkeypatch, tmp_path):
    import app.api.upload as upload_api

    async def fake_check_agent_access(db, current_user, agent_id):
        return SimpleNamespace(id=agent_id, owner_user_id=current_user.id, tenant_id=current_user.tenant_id), "write"

    class _FailingPersonalKnowledgeService:
        async def queue_source_bytes_import(self, *args, **kwargs):
            raise AssertionError("oversized upload must not queue into Personal KB")

    monkeypatch.setattr(upload_api, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(upload_api, "CHAT_UPLOAD_MAX_BYTES", 4)
    monkeypatch.setattr(upload_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(
        upload_api, "PersonalKnowledgeService", lambda: _FailingPersonalKnowledgeService(), raising=False
    )

    agent_id = uuid.uuid4()
    file = UploadFile(io.BytesIO(b"12345"), filename="large.md")
    with pytest.raises(HTTPException) as exc:
        await upload_api.upload_file(
            background_tasks=None,
            file=file,
            agent_id=agent_id,
            skip_personal_kb=False,
            current_user=SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4()),
            db=object(),
        )

    assert exc.value.status_code == 413
    assert list(tmp_path.rglob("*")) == []
