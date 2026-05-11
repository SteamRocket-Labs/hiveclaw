from __future__ import annotations

import json

import pytest


def test_office_document_service_rejects_path_escape(tmp_path):
    from app.services.office_document_service import OfficeDocumentPathError, OfficeDocumentService

    service = OfficeDocumentService(tmp_path)

    with pytest.raises(OfficeDocumentPathError):
        service.resolve_document_path("../outside.docx")

    with pytest.raises(OfficeDocumentPathError):
        service.resolve_document_path("/tmp/outside.docx")


def test_office_document_service_atomic_save_creates_revision_and_manifest(tmp_path):
    from app.services.office_document_service import OfficeDocumentService

    service = OfficeDocumentService(tmp_path)
    target = tmp_path / "workspace" / "demo.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-version")

    result = service.atomic_save_bytes("workspace/demo.docx", b"new-version", reason="callback-save")

    assert target.read_bytes() == b"new-version"
    assert result["path"] == "workspace/demo.docx"
    assert result["version"] == 1

    manifest_path = service.manifest_path("workspace/demo.docx")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["path"] == "workspace/demo.docx"
    assert manifest["current_version"] == 1
    assert manifest["revisions"][0]["reason"] == "callback-save"

    revision_path = manifest_path.parent / "revisions" / manifest["revisions"][0]["file"]
    assert revision_path.read_bytes() == b"old-version"


def test_office_document_service_refuses_write_when_editor_session_is_active(tmp_path):
    from app.services.office_document_service import (
        OfficeDocumentActiveSessionError,
        OfficeDocumentService,
    )

    service = OfficeDocumentService(tmp_path)
    target = tmp_path / "workspace" / "demo.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-version")
    service.set_active_editor_session("workspace/demo.docx", session_id="session-1", user_id="user-1")

    with pytest.raises(OfficeDocumentActiveSessionError) as exc:
        service.atomic_save_bytes("workspace/demo.docx", b"new-version", reason="agent-apply")

    assert exc.value.error_code == "active_editor_session"
    assert target.read_bytes() == b"old-version"


def test_office_document_service_allows_write_after_editor_session_closes(tmp_path):
    from app.services.office_document_service import OfficeDocumentService

    service = OfficeDocumentService(tmp_path)
    target = tmp_path / "workspace" / "demo.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-version")
    service.set_active_editor_session("workspace/demo.docx", session_id="session-1", user_id="user-1")
    service.clear_active_editor_session("workspace/demo.docx", session_id="session-1")

    service.atomic_save_bytes("workspace/demo.docx", b"new-version", reason="agent-apply")

    assert target.read_bytes() == b"new-version"
