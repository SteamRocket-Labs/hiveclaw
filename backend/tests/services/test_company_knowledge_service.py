from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.company_knowledge_service import CompanyKnowledgeImportError, CompanyKnowledgeService


class _FlushOnlySession:
    def __init__(self) -> None:
        self.flush_count = 0

    async def flush(self) -> None:
        self.flush_count += 1


def _wired_result(kwargs: dict):
    from app.services.document_conversion import DocumentConversionResult

    return DocumentConversionResult(
        markdown="# Wired\n\nCompany process seam body.",
        plain_text="Wired Company process seam body.",
        source_path="",
        source_uri=kwargs.get("source_uri"),
        source_sha256="c" * 64,
        source_mime_type=kwargs.get("source_mime_type") or "text/markdown",
        engine="killable-process-test",
        used_ocr=False,
        used_vision=False,
        page_count=1,
        artifact_markdown_path="",
        artifact_metadata_path="",
        warnings=(),
    )


@pytest.mark.asyncio
async def test_default_conversion_path_uses_killable_process_seam(monkeypatch, tmp_path: Path) -> None:
    """Default production construction routes conversion through the shared
    killable process seam; the thread path is only reachable via test DI."""
    import app.services.document_conversion as document_conversion

    seam_calls: list[dict] = []

    async def _fake_seam(**kwargs):
        seam_calls.append(kwargs)
        return _wired_result(kwargs)

    def _forbidden_to_thread(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("default production path must not use asyncio.to_thread")

    monkeypatch.setattr(document_conversion, "convert_bytes_in_killable_process", _fake_seam)
    monkeypatch.setattr(asyncio, "to_thread", _forbidden_to_thread)

    tenant_id = uuid.uuid4()
    job = SimpleNamespace(id=uuid.uuid4(), accountable_user_id=uuid.uuid4())
    request = {
        "direct_file_import": {"source_filename": "wire.md", "source_mime_type": "text/markdown"},
        "evidence_id": str(uuid.uuid4()),
    }
    service = CompanyKnowledgeService(data_root=tmp_path, conversion_timeout_seconds=7.5)

    canonical, canonical_path = await service._convert_direct_file_payload(
        _FlushOnlySession(),
        job=job,
        request=request,
        payload=b"# raw company body",
        tenant_id=tenant_id,
    )

    assert seam_calls == [
        {
            "data": b"# raw company body",
            "filename": "wire.md",
            "workspace_root": tmp_path / "companies" / str(tenant_id) / "knowledge" / "conversion",
            "timeout_seconds": 7.5,
            "source_uri": f"company-import://{job.id}/source",
            "source_mime_type": "text/markdown",
            "tenant_id": tenant_id,
            "agent_id": None,
            "user_id": job.accountable_user_id,
            "mode": "auto",
            "force_refresh": False,
        }
    ]
    assert canonical.decode("utf-8").startswith("# Wired")
    assert canonical_path.read_text(encoding="utf-8").startswith("# Wired")
    assert job.artifact_ref == str(canonical_path)
    assert request["conversion_receipt"]["engine"] == "killable-process-test"


@pytest.mark.asyncio
async def test_default_conversion_path_seam_timeout_stays_typed(monkeypatch, tmp_path: Path) -> None:
    """A TimeoutError from the killable seam keeps the exact conversion_timeout code."""
    import app.services.document_conversion as document_conversion

    async def _timeout_seam(**kwargs):
        raise TimeoutError("physical kill")

    monkeypatch.setattr(document_conversion, "convert_bytes_in_killable_process", _timeout_seam)

    service = CompanyKnowledgeService(data_root=tmp_path, conversion_timeout_seconds=0.05)
    job = SimpleNamespace(id=uuid.uuid4(), accountable_user_id=uuid.uuid4())
    request = {
        "direct_file_import": {"source_filename": "slow.pdf", "source_mime_type": "application/pdf"},
        "evidence_id": str(uuid.uuid4()),
    }

    with pytest.raises(CompanyKnowledgeImportError) as excinfo:
        await service._convert_direct_file_payload(
            _FlushOnlySession(),
            job=job,
            request=request,
            payload=b"%PDF-1.4 fake bytes for seam timeout wiring",
            tenant_id=uuid.uuid4(),
        )
    assert excinfo.value.code == "conversion_timeout"
