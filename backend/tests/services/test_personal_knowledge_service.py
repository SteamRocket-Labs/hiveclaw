from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.services.personal_knowledge_access import AgentRuntimePrincipal, HumanBrowserPrincipal
from sqlalchemy.sql.dml import Delete, Update
from sqlalchemy.sql.selectable import Select

from app.models.audit import AuditLog
from app.models.knowledge import (
    KnowledgeAssertion,
    KnowledgeDocument,
    KnowledgeEntity,
    KnowledgeGrant,
    KnowledgeIndexJob,
    KnowledgeLink,
    KnowledgeSegment,
)
from app.services.personal_knowledge_jobs import DEFAULT_IMPORT_JOB_MAX_ATTEMPTS
from app.services.personal_knowledge_service import PersonalKnowledgeService


class _ScalarResult:
    def __init__(self, value=None) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def all(self):
        return []


class _RowsResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeAsyncSession:
    def __init__(self, existing_document=None) -> None:
        self.existing_document = existing_document
        self.added: list[object] = []
        self.deleted: list[object] = []
        self.executed: list[object] = []
        self.flush_count = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_count += 1

    async def delete(self, obj) -> None:
        self.deleted.append(obj)

    async def execute(self, statement):
        self.executed.append(statement)
        if isinstance(statement, Select):
            return _ScalarResult(self.existing_document)
        return _ScalarResult()


class _FakeConversionService:
    def __init__(self, markdown: str = "# Imported\n\nConverted content.", warnings: tuple[str, ...] = ()) -> None:
        self.markdown = markdown
        self.warnings = warnings
        self.calls: list[dict] = []

    def convert_bytes(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            markdown=self.markdown,
            plain_text=self.markdown.replace("# ", ""),
            source_path="/tmp/source/report.html",
            source_uri=kwargs.get("source_uri"),
            source_sha256="c" * 64,
            source_mime_type=kwargs.get("source_mime_type") or "text/html",
            engine="fake_converter",
            used_ocr=False,
            used_vision=False,
            page_count=None,
            artifact_markdown_path="persons/owner/kb/.hive/document_conversions/content.md",
            artifact_metadata_path="persons/owner/kb/.hive/document_conversions/metadata.json",
            warnings=self.warnings,
        )


class _FailingConversionService:
    def convert_bytes(self, **kwargs):  # pragma: no cover - queue tests assert this is not called
        raise AssertionError("queueing an import must not synchronously convert bytes")


class _NoopKnowledgeExtractor:
    async def extract_segment(self, *args, **kwargs):  # pragma: no cover - existing tests do not inspect extraction
        from app.services.personal_knowledge_extractor import KnowledgeExtractionResult

        return KnowledgeExtractionResult()


class _UsageKnowledgeExtractor:
    def __init__(self) -> None:
        self.calls = 0

    async def extract_segment(self, *args, **kwargs):
        from app.services.personal_knowledge_extractor import (
            KnowledgeExtractionEntity,
            KnowledgeExtractionResult,
        )

        self.calls += 1
        return KnowledgeExtractionResult(
            entities=[
                KnowledgeExtractionEntity(
                    canonical_name=f"Usage entity {self.calls}",
                    entity_type="topic",
                    confidence=0.8,
                )
            ],
            usage={"input_tokens": 20, "output_tokens": 5, "cache_read_input_tokens": 3},
            usage_tokens=22,
        )


class _NoUsageKnowledgeExtractor:
    def __init__(self) -> None:
        self.calls = 0

    async def extract_segment(self, *args, **kwargs):
        from app.services.personal_knowledge_extractor import KnowledgeExtractionResult

        self.calls += 1
        return KnowledgeExtractionResult()


class _BlockingKnowledgeExtractor:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def extract_segment(self, *args, **kwargs):
        from app.services.personal_knowledge_extractor import KnowledgeExtractionResult

        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        await self.release.wait()
        self.active -= 1
        return KnowledgeExtractionResult()


class _GraphKnowledgeExtractor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def extract_segment(self, *, segment, document, source_ref, tenant_id, owner_user_id, sensitivity):
        from app.services.personal_knowledge_extractor import (
            KnowledgeExtractionAssertion,
            KnowledgeExtractionEntity,
            KnowledgeExtractionLink,
            KnowledgeExtractionResult,
        )

        self.calls.append(
            {
                "segment_id": segment.id,
                "document_id": document.id,
                "source_ref": source_ref,
                "tenant_id": tenant_id,
                "owner_user_id": owner_user_id,
                "sensitivity": sensitivity,
            }
        )
        return KnowledgeExtractionResult(
            entities=[
                KnowledgeExtractionEntity(
                    canonical_name="Crypto x AI",
                    entity_type="topic",
                    aliases=("CryptoAI",),
                    description="Intersection of crypto and artificial intelligence.",
                    confidence=0.92,
                ),
                KnowledgeExtractionEntity(
                    canonical_name="MEV",
                    entity_type="concept",
                    aliases=("Maximal Extractable Value",),
                    confidence=0.88,
                ),
            ],
            assertions=[
                KnowledgeExtractionAssertion(
                    subject_text="Crypto x AI",
                    predicate="includes",
                    object_text="MEV opportunity analysis",
                    confidence=0.84,
                )
            ],
            links=[
                KnowledgeExtractionLink(
                    from_name="Crypto x AI",
                    from_type="topic",
                    to_name="MEV",
                    to_type="concept",
                    relation="related_to",
                    confidence=0.79,
                )
            ],
        )


class _FailingKnowledgeExtractor:
    async def extract_segment(self, *args, **kwargs):
        raise ValueError("malformed extractor output")


class _FakeMediaTranscriptionProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def transcribe_media(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            markdown="# Audio transcript\n\nFounder says Personal KB should keep source refs.",
            warnings=["fake_transcription"],
            metadata={"duration_seconds": 12.5, "cost_usd": 0.03},
            provider="fake_media_provider",
        )


class _FakeVectorProvider:
    def __init__(self, *, search_hits: list | None = None, fail_index: bool = False) -> None:
        self.search_hits = list(search_hits or [])
        self.fail_index = fail_index
        self.index_calls: list[dict] = []
        self.search_calls: list[dict] = []

    async def index_personal_segments(self, **kwargs):
        self.index_calls.append(kwargs)
        if self.fail_index:
            raise ValueError("vector provider offline")
        return {"indexed": len(kwargs.get("segments") or [])}

    async def search_personal_segments(self, **kwargs):
        self.search_calls.append(kwargs)
        return list(self.search_hits)


class _SearchSession:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.executed: list[object] = []

    async def execute(self, statement):
        self.executed.append(statement)
        return _RowsResult(self.rows)


class _QueuedSession:
    def __init__(self, results) -> None:
        self.results = list(results)
        self.executed: list[object] = []
        self.flush_count = 0

    async def execute(self, statement):
        self.executed.append(statement)
        if not self.results:
            return _RowsResult([])
        return _RowsResult(self.results.pop(0))

    async def flush(self) -> None:
        self.flush_count += 1


def test_personal_knowledge_artifact_path_is_person_scope_stable(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import personal_knowledge_artifact_path

    owner_id = uuid.uuid4()
    source_hash = "a" * 64

    path = personal_knowledge_artifact_path(tmp_path, owner_id, source_hash)

    assert path == tmp_path / "persons" / str(owner_id) / "kb" / "documents" / "aa" / f"{source_hash}.md"


@pytest.mark.asyncio
async def test_list_import_jobs_consumes_scalar_entities_from_sqlalchemy_result(tmp_path: Path) -> None:
    """PostgreSQL returns Row wrappers from Result.all(), not tuple instances."""
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    job = SimpleNamespace(
        id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        stage="indexed",
        status="ready",
        artifact_hash="a" * 64,
        error_message=None,
        attempt_count=1,
        job_metadata_json={"source_kind": "upload"},
        created_at=now,
        updated_at=now,
    )

    class _PostgresRowLike:
        def __getitem__(self, index: int):
            if index != 0:
                raise IndexError(index)
            return job

    class _EntityResult:
        def all(self):
            return [_PostgresRowLike()]

        def scalars(self):
            return _RowsResult([job])

    class _Session:
        async def execute(self, statement):
            return _EntityResult()

    summaries = await PersonalKnowledgeService(data_root=tmp_path).list_import_jobs(
        _Session(),
        tenant_id=tenant_id,
        owner_user_id=owner_id,
    )

    assert len(summaries) == 1
    assert summaries[0].job_id == job.id
    assert summaries[0].status == "ready"
    assert summaries[0].metadata == {"source_kind": "upload"}


@pytest.mark.asyncio
async def test_get_personal_document_source_preview_reads_queued_image(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    source_rel = Path("persons") / str(owner_id) / "kb" / "imports" / "aa" / "112233.png"
    source_path = tmp_path / source_rel
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"\x89PNG\r\nsource")
    document = SimpleNamespace(
        id=uuid.uuid4(),
        source_sha256="a" * 64,
        sensitivity="PL1_public",
        doc_metadata_json={
            "queued_source_path": source_rel.as_posix(),
            "source_filename": "112233.png",
            "source_mime_type": "image/png",
            "media_kind": "image",
        },
    )
    session = _QueuedSession([[(document, 0)]])
    service = PersonalKnowledgeService(data_root=tmp_path)

    preview = await service.get_personal_document_source_preview(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        document_id=document.id,
        principal=HumanBrowserPrincipal(user_id=owner_id),
    )

    assert preview is not None
    assert preview.filename == "112233.png"
    assert preview.mime_type == "image/png"
    assert preview.content == b"\x89PNG\r\nsource"


@pytest.mark.asyncio
async def test_get_personal_document_source_preview_rejects_path_escape(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    document = SimpleNamespace(
        id=uuid.uuid4(),
        source_sha256="a" * 64,
        sensitivity="PL1_public",
        doc_metadata_json={
            "queued_source_path": "../secret.png",
            "source_filename": "secret.png",
            "source_mime_type": "image/png",
            "media_kind": "image",
        },
    )
    session = _QueuedSession([[(document, 0)], []])
    service = PersonalKnowledgeService(data_root=tmp_path)

    preview = await service.get_personal_document_source_preview(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        document_id=document.id,
        principal=HumanBrowserPrincipal(user_id=owner_id),
    )

    assert preview is None


def test_segment_markdown_preserves_heading_path_and_stable_hashes() -> None:
    from app.services.personal_knowledge_service import segment_markdown

    markdown = "# Root\n\nIntro paragraph.\n\n## Alpha\n\n" + ("alpha detail " * 80) + "\n\n## Beta\n\nbeta detail"

    first = segment_markdown(markdown, max_segment_chars=220, overlap_chars=30)
    second = segment_markdown(markdown, max_segment_chars=220, overlap_chars=30)

    assert [segment.segment_hash for segment in first] == [segment.segment_hash for segment in second]
    assert any(segment.heading_path == ["Root", "Alpha"] for segment in first)
    assert any("alpha detail" in segment.content for segment in first)
    assert all(segment.content.strip() for segment in first)


def test_personal_knowledge_facade_exports_are_owned_by_four_components() -> None:
    from app.services.personal_knowledge_service import (
        build_personal_knowledge_document_list_statement,
        build_personal_knowledge_job_claim_statement,
        build_personal_knowledge_search_statement,
        segment_markdown,
    )

    assert build_personal_knowledge_document_list_statement.__module__.endswith("personal_knowledge_access")
    assert segment_markdown.__module__.endswith("personal_knowledge_ingest")
    assert build_personal_knowledge_search_statement.__module__.endswith("personal_knowledge_index_search")
    assert build_personal_knowledge_job_claim_statement.__module__.endswith("personal_knowledge_jobs")


@pytest.mark.asyncio
async def test_ingest_markdown_writes_artifact_document_segments_and_index_job(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    user_id = owner_id
    session = _FakeAsyncSession()
    service = PersonalKnowledgeService(data_root=tmp_path, extractor=_NoopKnowledgeExtractor())

    result = await service.ingest_markdown(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        title="Taste notes",
        markdown="# Taste\n\nPrefer concise specs.\n\n## Retrieval\n\nUse source refs and ACL first.",
        source_kind="paste",
        source_uri="clipboard://test",
        created_by_user_id=user_id,
    )

    assert result.document_id is not None
    assert result.segment_count >= 1
    artifact_path = tmp_path / result.canonical_md_path
    assert artifact_path.exists()
    assert "Prefer concise specs." in artifact_path.read_text(encoding="utf-8")

    documents = [obj for obj in session.added if isinstance(obj, KnowledgeDocument)]
    segments = [obj for obj in session.added if isinstance(obj, KnowledgeSegment)]
    jobs = [obj for obj in session.added if isinstance(obj, KnowledgeIndexJob)]

    assert len(documents) == 1
    assert documents[0].tenant_id == tenant_id
    assert documents[0].scope_type == "person"
    assert documents[0].scope_id == owner_id
    assert documents[0].status == "ready"
    assert documents[0].agent_searchable is True
    assert len(segments) == result.segment_count
    assert len(jobs) == 1
    assert any(isinstance(statement, Delete) for statement in session.executed)
    assert any(isinstance(statement, Update) for statement in session.executed)


@pytest.mark.asyncio
async def test_ingest_markdown_records_optional_vector_unconfigured_without_pgvector_dependency(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    session = _FakeAsyncSession()
    service = PersonalKnowledgeService(data_root=tmp_path, extractor=_NoopKnowledgeExtractor())

    await service.ingest_markdown(
        session,
        tenant_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        title="No vector provider",
        markdown="# Source\n\nPersonal M1 must boot without pgvector.",
        source_kind="paste",
    )

    jobs = [obj for obj in session.added if isinstance(obj, KnowledgeIndexJob)]
    assert jobs[-1].job_metadata_json["optional_vector"] == {
        "enabled": False,
        "status": "disabled",
        "reason": "provider_unconfigured",
    }
    assert "vector" not in jobs[-1].job_metadata_json["channels"]


@pytest.mark.asyncio
async def test_ingest_markdown_indexes_optional_vector_provider_from_segments(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    provider = _FakeVectorProvider()
    session = _FakeAsyncSession()
    service = PersonalKnowledgeService(
        data_root=tmp_path,
        extractor=_NoopKnowledgeExtractor(),
        vector_provider=provider,
    )

    result = await service.ingest_markdown(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        title="Vector source",
        markdown="# Vector\n\nSemantic lane consumes canonical segment text.",
        source_kind="paste",
    )

    jobs = [obj for obj in session.added if isinstance(obj, KnowledgeIndexJob)]
    assert provider.index_calls
    assert provider.index_calls[0]["tenant_id"] == tenant_id
    assert provider.index_calls[0]["owner_user_id"] == owner_id
    assert provider.index_calls[0]["document_id"] == result.document_id
    assert provider.index_calls[0]["segments"][0]["heading_path"] == ["Vector"]
    assert provider.index_calls[0]["segments"][0]["index_text"].startswith("Vector source\nVector")
    assert "Semantic lane consumes canonical segment text." in provider.index_calls[0]["segments"][0]["index_text"]
    assert jobs[-1].job_metadata_json["optional_vector"]["status"] == "ready"
    assert "vector" in jobs[-1].job_metadata_json["channels"]


@pytest.mark.asyncio
async def test_ingest_source_bytes_converts_file_then_indexes_canonical_markdown(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    converter = _FakeConversionService(markdown="# Imported\n\nConverted through canonical MD.")
    session = _FakeAsyncSession()
    service = PersonalKnowledgeService(
        data_root=tmp_path,
        conversion_service=converter,
        extractor=_NoopKnowledgeExtractor(),
    )

    result = await service.ingest_source_bytes(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        filename="report.html",
        data=b"<h1>raw</h1>",
        source_kind="upload",
        source_uri="upload://report.html",
        created_by_user_id=owner_id,
        agent_searchable=True,
        sensitivity="internal",
    )

    assert result.status == "ready"
    assert result.job_id is not None
    assert result.source_sha256 == "c" * 64
    assert converter.calls[0]["filename"] == "report.html"
    assert converter.calls[0]["workspace_root"] == tmp_path / "persons" / str(owner_id) / "kb"
    assert (tmp_path / result.canonical_md_path).read_text(encoding="utf-8").startswith("# Imported")
    jobs = [obj for obj in session.added if isinstance(obj, KnowledgeIndexJob)]
    assert jobs[-1].status == "ready"
    assert jobs[-1].stage == "indexed"
    assert jobs[-1].job_metadata_json["source_kind"] == "upload"


@pytest.mark.asyncio
async def test_queue_source_bytes_import_writes_spool_job_without_conversion(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    session = _FakeAsyncSession()
    service = PersonalKnowledgeService(
        data_root=tmp_path,
        conversion_service=_FailingConversionService(),
        extractor=_FailingKnowledgeExtractor(),
    )

    result = await service.queue_source_bytes_import(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        filename="report.pdf",
        data=b"%PDF queued bytes",
        title="Queued report",
        source_kind="upload",
        source_uri="upload://report.pdf",
        source_mime_type="application/pdf",
        created_by_user_id=owner_id,
        agent_searchable=True,
        sensitivity="internal",
        doc_metadata={"source_context": "unit-test"},
    )

    documents = [obj for obj in session.added if isinstance(obj, KnowledgeDocument)]
    jobs = [obj for obj in session.added if isinstance(obj, KnowledgeIndexJob)]
    spool_path = tmp_path / jobs[-1].job_metadata_json["queued_source_path"]

    assert result.status == "queued"
    assert result.segment_count == 0
    assert documents[-1].status == "queued"
    assert documents[-1].source_sha256 == result.source_sha256
    assert documents[-1].doc_metadata_json["queued_import_kind"] == "source_bytes"
    assert jobs[-1].stage == "queued"
    assert jobs[-1].status == "queued"
    assert jobs[-1].attempt_count == 0
    assert jobs[-1].job_metadata_json["queued_import_kind"] == "source_bytes"
    assert jobs[-1].job_metadata_json["source_filename"] == "report.pdf"
    assert spool_path.read_bytes() == b"%PDF queued bytes"


@pytest.mark.asyncio
async def test_ingest_source_bytes_records_failed_job_for_unsupported_file(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    session = _FakeAsyncSession()
    owner_id = uuid.uuid4()
    service = PersonalKnowledgeService(
        data_root=tmp_path,
        conversion_service=_FakeConversionService(),
        extractor=_NoopKnowledgeExtractor(),
    )

    result = await service.ingest_source_bytes(
        session,
        tenant_id=uuid.uuid4(),
        owner_user_id=owner_id,
        filename="archive.exe",
        data=b"binary",
        source_kind="upload",
        created_by_user_id=owner_id,
    )

    assert result.status == "failed"
    assert result.job_id is not None
    documents = [obj for obj in session.added if isinstance(obj, KnowledgeDocument)]
    jobs = [obj for obj in session.added if isinstance(obj, KnowledgeIndexJob)]
    assert documents[-1].status == "failed"
    assert jobs[-1].status == "failed"
    assert "unsupported_file_type" in jobs[-1].error_message


@pytest.mark.asyncio
async def test_ingest_markdown_writes_extracted_graph_with_source_refs(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    extractor = _GraphKnowledgeExtractor()
    session = _FakeAsyncSession()
    service = PersonalKnowledgeService(data_root=tmp_path, extractor=extractor)

    result = await service.ingest_markdown(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        title="Crypto AI notes",
        markdown="# Crypto x AI\n\nMEV is one of the recurring opportunity areas.",
        source_kind="paste",
        created_by_user_id=owner_id,
    )

    entities = [obj for obj in session.added if isinstance(obj, KnowledgeEntity)]
    assertions = [obj for obj in session.added if isinstance(obj, KnowledgeAssertion)]
    links = [obj for obj in session.added if isinstance(obj, KnowledgeLink)]
    jobs = [obj for obj in session.added if isinstance(obj, KnowledgeIndexJob)]

    assert result.status == "ready"
    assert extractor.calls
    assert len(entities) == 2
    assert {entity.canonical_name for entity in entities} == {"Crypto x AI", "MEV"}
    assert entities[0].source_refs_json[0]["document_id"] == str(result.document_id)
    assert entities[0].source_refs_json[0]["segment_id"] == str(extractor.calls[0]["segment_id"])
    assert len(assertions) == 1
    assert assertions[0].source_document_id == result.document_id
    assert assertions[0].source_refs_json[0]["seg_hash"]
    assert len(links) == 1
    assert links[0].relation == "related_to"
    assert links[0].source_refs_json[0]["heading_path"] == ["Crypto x AI"]
    assert jobs[-1].stage == "indexed"
    assert jobs[-1].status == "ready"
    assert "graph" in jobs[-1].job_metadata_json["channels"]


@pytest.mark.asyncio
async def test_ingest_markdown_records_extraction_usage_in_job_metadata(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    extractor = _UsageKnowledgeExtractor()
    session = _FakeAsyncSession()
    service = PersonalKnowledgeService(data_root=tmp_path, extractor=extractor)

    await service.ingest_markdown(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        title="Usage notes",
        markdown="# One\n\nFirst segment.\n\n## Two\n\nSecond segment.",
        source_kind="paste",
        created_by_user_id=owner_id,
    )

    jobs = [obj for obj in session.added if isinstance(obj, KnowledgeIndexJob)]
    usage = jobs[-1].job_metadata_json["extraction_usage"]

    assert usage["segment_count"] == extractor.calls
    assert usage["segments_with_usage"] == extractor.calls
    assert usage["tokens"] == 22 * extractor.calls
    assert usage["provider_usage"]["input_tokens"] == 20 * extractor.calls
    assert usage["provider_usage"]["output_tokens"] == 5 * extractor.calls
    assert usage["provider_usage"]["cache_read_input_tokens"] == 3 * extractor.calls


@pytest.mark.asyncio
async def test_ingest_markdown_records_usage_unavailable_when_provider_omits_usage(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    extractor = _NoUsageKnowledgeExtractor()
    session = _FakeAsyncSession()
    service = PersonalKnowledgeService(data_root=tmp_path, extractor=extractor)

    await service.ingest_markdown(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        title="No usage notes",
        markdown="# One\n\nProvider did not return usage.",
        source_kind="paste",
        created_by_user_id=owner_id,
    )

    jobs = [obj for obj in session.added if isinstance(obj, KnowledgeIndexJob)]
    usage = jobs[-1].job_metadata_json["extraction_usage"]

    assert usage["segment_count"] == extractor.calls
    assert usage["segments_with_usage"] == 0
    assert usage["usage_unavailable_count"] == extractor.calls
    assert usage["tokens"] == 0


@pytest.mark.asyncio
async def test_extract_segment_guard_limits_same_tenant_concurrency(monkeypatch, tmp_path: Path) -> None:
    import app.services.personal_knowledge_ingest as ingest_module
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    extractor = _BlockingKnowledgeExtractor()
    service = PersonalKnowledgeService(data_root=tmp_path, extractor=extractor)
    document = SimpleNamespace(id=uuid.uuid4(), title="Doc", source_kind="paste")
    segment_a = SimpleNamespace(id=uuid.uuid4(), heading_path_json=[], content="A")
    segment_b = SimpleNamespace(id=uuid.uuid4(), heading_path_json=[], content="B")

    monkeypatch.setattr(ingest_module, "DEFAULT_EXTRACT_MAX_CONCURRENCY_PER_TENANT", 1)
    ingest_module._EXTRACT_SEMAPHORES.clear()

    first = asyncio.create_task(
        service._extract_segment_with_tenant_guard(
            extractor=extractor,
            segment=segment_a,
            document=document,
            source_ref={"segment_id": str(segment_a.id)},
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            sensitivity="internal",
        )
    )
    await extractor.entered.wait()
    second = asyncio.create_task(
        service._extract_segment_with_tenant_guard(
            extractor=extractor,
            segment=segment_b,
            document=document,
            source_ref={"segment_id": str(segment_b.id)},
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            sensitivity="internal",
        )
    )
    await asyncio.sleep(0)

    assert extractor.max_active == 1
    assert not second.done()
    extractor.release.set()
    await asyncio.gather(first, second)
    assert extractor.max_active == 1


@pytest.mark.asyncio
async def test_ingest_markdown_marks_document_degraded_when_extraction_fails(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    session = _FakeAsyncSession()
    service = PersonalKnowledgeService(data_root=tmp_path, extractor=_FailingKnowledgeExtractor())

    result = await service.ingest_markdown(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        title="Broken extraction",
        markdown="# Source\n\nThe canonical markdown and search segment must remain available.",
        source_kind="paste",
        created_by_user_id=owner_id,
    )

    documents = [obj for obj in session.added if isinstance(obj, KnowledgeDocument)]
    segments = [obj for obj in session.added if isinstance(obj, KnowledgeSegment)]
    entities = [obj for obj in session.added if isinstance(obj, KnowledgeEntity)]
    jobs = [obj for obj in session.added if isinstance(obj, KnowledgeIndexJob)]

    assert result.status == "degraded"
    assert "knowledge_extraction_failed:malformed extractor output" in result.warnings
    assert documents[-1].status == "degraded"
    assert segments
    assert entities == []
    assert jobs[-1].stage == "extracting"
    assert jobs[-1].status == "degraded"
    assert "malformed extractor output" in jobs[-1].error_message


@pytest.mark.asyncio
async def test_ingest_markdown_canonicalizes_pl3_and_skips_graph_extraction(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    extractor = _UsageKnowledgeExtractor()
    session = _FakeAsyncSession()
    service = PersonalKnowledgeService(data_root=tmp_path, extractor=extractor)

    result = await service.ingest_markdown(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        title="Confidential operating note",
        markdown="# Restricted\n\nThis authorized source stays searchable but must not enter the graph.",
        source_kind="paste",
        created_by_user_id=owner_id,
        sensitivity="confidential",
    )

    documents = [obj for obj in session.added if isinstance(obj, KnowledgeDocument)]
    jobs = [obj for obj in session.added if isinstance(obj, KnowledgeIndexJob)]

    assert extractor.calls == 0
    assert result.status == "degraded"
    assert result.warnings == ["knowledge_extraction_skipped_sensitive"]
    assert documents[-1].sensitivity == "PL3_sensitive"
    assert jobs[-1].error_message == "knowledge_extraction_skipped_sensitive"


@pytest.mark.asyncio
async def test_patch_and_rebuild_personal_document_index_update_existing_document(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    owner_id = uuid.uuid4()
    document = SimpleNamespace(
        id=uuid.uuid4(),
        title="Existing",
        source_kind="paste",
        source_uri=None,
        source_sha256="a" * 64,
        artifact_hash="b" * 64,
        status="ready",
        sensitivity="internal",
        agent_searchable=True,
        canonical_md_path="persons/owner/kb/doc.md",
        canonical_md_sha256="b" * 64,
        doc_metadata_json={},
        created_by_user_id=owner_id,
        created_at=None,
        updated_at=None,
    )
    session = _FakeAsyncSession(existing_document=document)
    service = PersonalKnowledgeService(data_root=tmp_path, extractor=_NoopKnowledgeExtractor())

    patched = await service.patch_personal_document(
        session,
        tenant_id=uuid.uuid4(),
        owner_user_id=owner_id,
        document_id=document.id,
        current_user_id=owner_id,
        agent_id=None,
        agent_searchable=False,
        sensitivity="private",
        status="archived",
    )

    assert patched is not None
    assert document.agent_searchable is False
    assert document.sensitivity == "PL3_sensitive"
    assert document.status == "archived"


def test_owner_search_statement_uses_person_scope_without_grant_requirement() -> None:
    from app.services.personal_knowledge_service import build_personal_knowledge_search_statement

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()

    statement = build_personal_knowledge_search_statement(
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        query="source refs",
        principal=HumanBrowserPrincipal(user_id=owner_id),
        limit=5,
    )
    compiled = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))

    assert "knowledge_documents.scope_type" in compiled
    assert "knowledge_documents.scope_id" in compiled
    assert "knowledge_segments.tsv @@ plainto_tsquery" in compiled
    assert "knowledge_grants" not in compiled
    assert "knowledge_documents.agent_searchable IS true" not in compiled


def test_external_search_statement_requires_matching_user_or_agent_grant() -> None:
    from app.services.personal_knowledge_service import build_personal_knowledge_search_statement

    statement = build_personal_knowledge_search_statement(
        tenant_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        query="source refs",
        principal=AgentRuntimePrincipal(
            agent_id=uuid.uuid4(),
            requester_user_id=uuid.uuid4(),
            session_id="cross-principal-search",
        ),
        limit=5,
    )
    compiled = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))

    assert "knowledge_grants" in compiled
    assert "knowledge_grants.grantee_type" in compiled
    assert "knowledge_grants.resource_type" in compiled
    assert "knowledge_grants.permission IN" in compiled
    assert "knowledge_documents.agent_searchable IS true" in compiled


def test_interactive_owner_agent_search_uses_requester_bound_owner_chain() -> None:
    from app.services.personal_knowledge_service import build_personal_knowledge_search_statement

    owner_id = uuid.uuid4()
    statement = build_personal_knowledge_search_statement(
        tenant_id=uuid.uuid4(),
        owner_user_id=owner_id,
        query="source refs",
        principal=AgentRuntimePrincipal(
            agent_id=uuid.uuid4(),
            requester_user_id=owner_id,
            session_id="owner-interactive-search",
        ),
        limit=5,
    )
    compiled = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))

    assert "agents.id" in compiled
    assert "agents.tenant_id" in compiled
    assert "agents.deleted_at IS NULL" in compiled
    assert "agents.owner_user_id" in compiled
    assert "agents.sponsor_user_id" not in compiled
    assert "agents.creator_id" in compiled
    assert "knowledge_grants" in compiled
    assert "knowledge_documents.agent_searchable IS true" in compiled


def test_personal_document_list_statement_requires_grant_for_non_owner() -> None:
    from app.services.personal_knowledge_service import build_personal_knowledge_document_list_statement

    statement = build_personal_knowledge_document_list_statement(
        tenant_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        principal=AgentRuntimePrincipal(
            agent_id=uuid.uuid4(),
            requester_user_id=uuid.uuid4(),
            session_id="cross-principal-read",
        ),
        limit=25,
    )
    compiled = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))

    assert "knowledge_documents.scope_type" in compiled
    assert "knowledge_documents.scope_id" in compiled
    assert "knowledge_grants" in compiled
    assert "knowledge_grants.permission IN" in compiled
    assert "knowledge_documents.agent_searchable IS true" in compiled


def test_agent_document_detail_statement_requires_agent_searchable_for_non_owner() -> None:
    from app.services.personal_knowledge_service import build_personal_knowledge_document_list_statement

    owner_id = uuid.uuid4()
    statement = build_personal_knowledge_document_list_statement(
        tenant_id=uuid.uuid4(),
        owner_user_id=owner_id,
        principal=AgentRuntimePrincipal(
            agent_id=uuid.uuid4(),
            requester_user_id=owner_id,
            session_id="owner-detail-read",
        ),
        limit=1,
        document_id=uuid.uuid4(),
    )
    compiled = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))

    assert "knowledge_documents.id" in compiled
    assert "agents.owner_user_id" in compiled
    assert "knowledge_documents.agent_searchable IS true" in compiled


def test_personal_document_list_statement_does_not_require_grant_for_owner() -> None:
    from app.services.personal_knowledge_service import build_personal_knowledge_document_list_statement

    owner_id = uuid.uuid4()
    statement = build_personal_knowledge_document_list_statement(
        tenant_id=uuid.uuid4(),
        owner_user_id=owner_id,
        principal=HumanBrowserPrincipal(user_id=owner_id),
        limit=25,
    )
    compiled = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))

    assert "knowledge_documents.scope_type" in compiled
    assert "knowledge_grants" not in compiled


@pytest.mark.asyncio
async def test_list_personal_documents_maps_document_summary_rows(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    document = SimpleNamespace(
        id=uuid.uuid4(),
        title="Taste notes",
        source_kind="paste",
        source_uri="clipboard://taste",
        source_sha256="a" * 64,
        canonical_md_path="persons/owner/kb/doc.md",
        status="ready",
        sensitivity="internal",
        agent_searchable=True,
        doc_metadata_json={"ingest_format": "canonical_markdown"},
        created_at=None,
        updated_at=None,
    )
    session = _SearchSession(rows=[(document, 3)])
    service = PersonalKnowledgeService(data_root=tmp_path)

    summaries = await service.list_personal_documents(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        principal=HumanBrowserPrincipal(user_id=owner_id),
        limit=10,
    )

    assert len(summaries) == 1
    assert summaries[0].document_id == document.id
    assert summaries[0].title == "Taste notes"
    assert summaries[0].segment_count == 3
    assert summaries[0].source_ref == f"kb://person/{owner_id}/documents/{document.id}"


@pytest.mark.asyncio
async def test_get_personal_document_maps_segments_under_same_acl(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    document_id = uuid.uuid4()
    document = SimpleNamespace(
        id=document_id,
        title="Retrieval notes",
        source_kind="paste",
        source_uri=None,
        source_sha256="b" * 64,
        canonical_md_path="persons/owner/kb/doc.md",
        status="ready",
        sensitivity="internal",
        agent_searchable=True,
        doc_metadata_json={},
        created_at=None,
        updated_at=None,
    )
    segment = SimpleNamespace(
        id=uuid.uuid4(),
        position=0,
        heading_path_json=["Retrieval"],
        content="Use source refs and ACL before context injection.",
        token_count=8,
    )
    session = _QueuedSession(results=[[(document, 1)], [segment]])
    service = PersonalKnowledgeService(data_root=tmp_path)

    detail = await service.get_personal_document(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        document_id=document_id,
        principal=HumanBrowserPrincipal(user_id=owner_id),
    )

    assert detail is not None
    assert detail.document_id == document_id
    assert detail.segment_count == 1
    assert detail.segments[0].segment_id == segment.id
    assert detail.segments[0].heading_path == ["Retrieval"]


@pytest.mark.asyncio
async def test_search_personal_maps_rows_to_source_ref_hits(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    document = SimpleNamespace(
        id=uuid.uuid4(),
        title="Retrieval notes",
        source_sha256="b" * 64,
        canonical_md_path="persons/owner/kb/doc.md",
        sensitivity="internal",
    )
    segment = SimpleNamespace(
        id=uuid.uuid4(),
        heading_path_json=["Retrieval"],
        content="Use source refs and ACL before context injection.",
    )
    session = _SearchSession(rows=[(segment, document, 0.87)])
    service = PersonalKnowledgeService(data_root=tmp_path)

    hits = await service.search_personal(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        query="source refs",
        principal=HumanBrowserPrincipal(user_id=owner_id),
        limit=3,
    )

    assert len(hits) == 1
    assert hits[0].document_id == document.id
    assert hits[0].segment_id == segment.id
    assert hits[0].title == "Retrieval notes"
    assert hits[0].source_ref == f"kb://person/{owner_id}/documents/{document.id}#segment={segment.id}"
    assert hits[0].score > 0
    assert hits[0].score_trace["channels"]["text"]["raw_score"] == pytest.approx(0.87)
    assert hits[0].score_trace["channels"]["text"]["rank"] == 1
    assert session.executed


@pytest.mark.asyncio
async def test_search_personal_fuses_entity_and_graph_channels_with_score_trace(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    entity_segment = SimpleNamespace(
        id=uuid.uuid4(),
        heading_path_json=["Crypto x AI"],
        content="Crypto x AI notes mention MEV strategy.",
    )
    graph_segment = SimpleNamespace(
        id=uuid.uuid4(),
        heading_path_json=["MEV"],
        content="MEV is connected to builder/searcher workflows.",
    )
    document = SimpleNamespace(
        id=uuid.uuid4(),
        title="Crypto AI memo",
        source_sha256="d" * 64,
        canonical_md_path="persons/owner/kb/doc.md",
        sensitivity="internal",
        doc_metadata_json={"citation_count": 3},
        updated_at=None,
    )
    entity = SimpleNamespace(
        id=uuid.uuid4(),
        canonical_name="Crypto x AI",
        aliases_json=["CryptoAI"],
        source_refs_json=[{"segment_id": str(entity_segment.id), "document_id": str(document.id)}],
    )
    link = SimpleNamespace(
        id=uuid.uuid4(),
        from_id=entity.id,
        to_id=uuid.uuid4(),
        relation="related_to",
        source_refs_json=[{"segment_id": str(graph_segment.id), "document_id": str(document.id)}],
    )
    session = _QueuedSession(results=[[], [entity], [link], [(entity_segment, document), (graph_segment, document)]])
    service = PersonalKnowledgeService(data_root=tmp_path)

    hits = await service.search_personal(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        query="CryptoAI MEV",
        principal=HumanBrowserPrincipal(user_id=owner_id),
        limit=5,
    )

    assert [hit.segment_id for hit in hits] == [entity_segment.id, graph_segment.id]
    assert hits[0].score_trace["channels"]["entity"]["rank"] == 1
    assert hits[0].score_trace["boosts"]["heat"] > 0
    assert hits[1].score_trace["channels"]["graph"]["rank"] == 1
    assert hits[0].score >= hits[1].score


@pytest.mark.asyncio
async def test_search_personal_graph_channel_uses_multihop_ppr_scores(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    document = SimpleNamespace(
        id=uuid.uuid4(),
        title="Associative retrieval memo",
        source_sha256="e" * 64,
        canonical_md_path="persons/owner/kb/assoc.md",
        sensitivity="internal",
        doc_metadata_json={},
        updated_at=None,
    )
    seed_segment = SimpleNamespace(
        id=uuid.uuid4(),
        heading_path_json=["Seed"],
        content="Open Notebook captures source grounded personal knowledge.",
    )
    two_hop_segment = SimpleNamespace(
        id=uuid.uuid4(),
        heading_path_json=["Two hop"],
        content="NotebookLM comparison should be recalled through the adjacent project link.",
    )
    entity_a_id = uuid.uuid4()
    entity_b_id = uuid.uuid4()
    entity_c_id = uuid.uuid4()
    entity_a = SimpleNamespace(
        id=entity_a_id,
        canonical_name="Open Notebook",
        aliases_json=["OpenNotebook"],
        confidence=0.9,
        source_refs_json=[{"segment_id": str(seed_segment.id), "document_id": str(document.id)}],
    )
    link_ab = SimpleNamespace(
        id=uuid.uuid4(),
        from_id=entity_a_id,
        to_id=entity_b_id,
        confidence=0.9,
        relation="inspired_by",
        source_refs_json=[],
    )
    link_bc = SimpleNamespace(
        id=uuid.uuid4(),
        from_id=entity_b_id,
        to_id=entity_c_id,
        confidence=0.8,
        relation="compares_with",
        source_refs_json=[{"segment_id": str(two_hop_segment.id), "document_id": str(document.id)}],
    )
    session = _QueuedSession(
        results=[[], [entity_a], [link_ab, link_bc], [(seed_segment, document), (two_hop_segment, document)]]
    )
    service = PersonalKnowledgeService(data_root=tmp_path)

    hits = await service.search_personal(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        query="OpenNotebook",
        principal=HumanBrowserPrincipal(user_id=owner_id),
        limit=5,
    )

    assert [hit.segment_id for hit in hits] == [seed_segment.id, two_hop_segment.id]
    graph_trace = hits[1].score_trace["channels"]["graph"]
    assert graph_trace["rank"] == 1
    assert 0.0 < graph_trace["raw_score"] < 1.0
    assert graph_trace["method"] == "ppr"
    assert graph_trace["hops"] >= 2


@pytest.mark.asyncio
async def test_search_personal_fuses_optional_vector_provider_after_acl_fetch(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    vector_segment = SimpleNamespace(
        id=uuid.uuid4(),
        heading_path_json=["Semantic"],
        content="Notebook style imports should be recalled by semantic similarity.",
    )
    document = SimpleNamespace(
        id=uuid.uuid4(),
        title="Open Notebook comparison",
        source_sha256="f" * 64,
        canonical_md_path="persons/owner/kb/vector.md",
        sensitivity="internal",
        doc_metadata_json={},
        updated_at=None,
        status="ready",
    )
    provider = _FakeVectorProvider(
        search_hits=[
            {
                "segment_id": str(vector_segment.id),
                "score": 0.91,
                "metadata": {"provider": "fake"},
            }
        ]
    )
    session = _QueuedSession([[], [], [(vector_segment, document)]])
    service = PersonalKnowledgeService(data_root=tmp_path, vector_provider=provider)

    hits = await service.search_personal(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        query="notebook source grounded imports",
        principal=HumanBrowserPrincipal(user_id=owner_id),
        limit=3,
    )

    assert [hit.segment_id for hit in hits] == [vector_segment.id]
    assert provider.search_calls[0]["query"] == "notebook source grounded imports"
    vector_trace = hits[0].score_trace["channels"]["optional_vector"]
    assert vector_trace["rank"] == 1
    assert vector_trace["raw_score"] == pytest.approx(0.91)
    assert vector_trace["provider"] == "fake"
    assert hits[0].score_trace["optional_vector"]["status"] == "ready"


@pytest.mark.asyncio
async def test_create_personal_grant_writes_owner_scope_grant(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    session = _FakeAsyncSession(existing_document=agent_id)
    service = PersonalKnowledgeService(data_root=tmp_path)

    grant = await service.create_personal_grant(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        current_user_id=owner_id,
        resource_type="scope",
        resource_id=owner_id,
        document_id=None,
        grantee_type="agent",
        grantee_id=agent_id,
        permission="search",
        requester_user_id=owner_id,
        purpose="autonomous_agent",
        sensitivity_ceiling="PL3_sensitive",
        expires_at=expires_at,
        grant_metadata={"reason": "research"},
    )

    added_grants = [obj for obj in session.added if isinstance(obj, KnowledgeGrant)]
    assert grant is not None
    assert grant.grantee_id == agent_id
    assert grant.permission == "search"
    assert grant.requester_user_id == owner_id
    assert grant.purpose == "autonomous_agent"
    assert grant.sensitivity_ceiling == "PL3_sensitive"
    assert grant.expires_at == expires_at
    assert grant.revoked_at is None
    assert added_grants[-1].scope_type == "person"
    assert added_grants[-1].scope_id == owner_id
    assert added_grants[-1].resource_type == "scope"
    assert added_grants[-1].resource_id == owner_id
    assert added_grants[-1].binding_key.startswith("pkb:")
    assert added_grants[-1].grant_metadata_json == {"reason": "research"}
    audit = next(obj for obj in session.added if isinstance(obj, AuditLog))
    assert audit.action == "personal_kb.grant.upserted"
    assert audit.tenant_id == tenant_id
    assert audit.user_id == owner_id
    assert audit.agent_id == agent_id
    assert audit.details == {
        "grant_id": str(added_grants[-1].id),
        "operation": "created",
        "resource_type": "scope",
        "resource_id": str(owner_id),
        "grantee_type": "agent",
        "grantee_id": str(agent_id),
        "permission": "search",
        "requester_user_id": str(owner_id),
        "session_id": None,
        "purpose": "autonomous_agent",
        "delegation_id": None,
        "sensitivity_ceiling": "PL3_sensitive",
        "expires_at": expires_at.isoformat(),
        "binding_key": added_grants[-1].binding_key,
    }


@pytest.mark.asyncio
async def test_agent_grant_rejects_unbounded_or_unbound_authority(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    service = PersonalKnowledgeService(data_root=tmp_path)
    common = {
        "tenant_id": uuid.uuid4(),
        "owner_user_id": uuid.uuid4(),
        "resource_type": "scope",
        "resource_id": None,
        "document_id": None,
        "grantee_type": "agent",
        "grantee_id": uuid.uuid4(),
        "permission": "read",
        "sensitivity_ceiling": "PL3_sensitive",
    }
    common["resource_id"] = common["owner_user_id"]

    with pytest.raises(ValueError, match="expires_at is required"):
        await service.create_personal_grant(
            _FakeAsyncSession(),
            current_user_id=common["owner_user_id"],
            requester_user_id=common["owner_user_id"],
            purpose="autonomous_agent",
            **common,
        )

    with pytest.raises(ValueError, match="autonomous_agent grants cannot carry session_id"):
        await service.create_personal_grant(
            _FakeAsyncSession(),
            current_user_id=common["owner_user_id"],
            requester_user_id=common["owner_user_id"],
            session_id="browser-session-must-not-bind-autonomous-authority",
            purpose="autonomous_agent",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            **common,
        )

    with pytest.raises(ValueError, match="session_id is required"):
        await service.create_personal_grant(
            _FakeAsyncSession(),
            current_user_id=common["owner_user_id"],
            requester_user_id=uuid.uuid4(),
            purpose="a2a_delegation",
            delegation_id="delegation-1",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            **common,
        )


@pytest.mark.asyncio
async def test_delete_personal_grant_is_auditable_soft_revoke(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    owner_id = uuid.uuid4()
    grant = KnowledgeGrant(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        scope_type="person",
        scope_id=owner_id,
        resource_type="scope",
        resource_id=owner_id,
        grantee_type="user",
        grantee_id=uuid.uuid4(),
        permission="read",
        sensitivity_ceiling="PL2_pii",
        binding_key="pkb:test",
        grant_metadata_json={"reason": "temporary review"},
    )
    session = _FakeAsyncSession(existing_document=grant)

    revoked = await PersonalKnowledgeService(data_root=tmp_path).delete_personal_grant(
        session,
        tenant_id=grant.tenant_id,
        owner_user_id=owner_id,
        current_user_id=owner_id,
        grant_id=grant.id,
    )

    assert revoked is True
    assert grant.revoked_at is not None
    assert grant.revoked_by_user_id == owner_id
    assert grant.grant_metadata_json["authority_status"] == "revoked"
    assert session.deleted == []
    audit = next(obj for obj in session.added if isinstance(obj, AuditLog))
    assert audit.action == "personal_kb.grant.revoked"
    assert audit.tenant_id == grant.tenant_id
    assert audit.user_id == owner_id
    assert audit.agent_id is None
    assert audit.details == {
        "grant_id": str(grant.id),
        "resource_type": "scope",
        "resource_id": str(owner_id),
        "grantee_type": "user",
        "grantee_id": str(grant.grantee_id),
        "permission": "read",
        "requester_user_id": None,
        "session_id": None,
        "purpose": None,
        "delegation_id": None,
        "sensitivity_ceiling": "PL2_pii",
        "revoked_at": grant.revoked_at.isoformat(),
        "binding_key": "pkb:test",
    }


@pytest.mark.asyncio
async def test_non_owner_cannot_create_personal_grant(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    session = _FakeAsyncSession()
    service = PersonalKnowledgeService(data_root=tmp_path)

    grant = await service.create_personal_grant(
        session,
        tenant_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        current_user_id=uuid.uuid4(),
        resource_type="scope",
        resource_id=uuid.uuid4(),
        document_id=None,
        grantee_type="agent",
        grantee_id=uuid.uuid4(),
        permission="search",
    )

    assert grant is None
    assert [obj for obj in session.added if isinstance(obj, KnowledgeGrant)] == []
    assert [obj for obj in session.added if isinstance(obj, AuditLog)] == []


@pytest.mark.asyncio
async def test_ingest_media_records_failed_job_when_provider_unconfigured(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    session = _FakeAsyncSession()
    service = PersonalKnowledgeService(data_root=tmp_path, extractor=None)

    result = await service.ingest_source_bytes(
        session,
        tenant_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        filename="meeting.mp3",
        data=b"fake audio",
        source_kind="upload",
        source_mime_type="audio/mpeg",
    )

    documents = [obj for obj in session.added if isinstance(obj, KnowledgeDocument)]
    jobs = [obj for obj in session.added if isinstance(obj, KnowledgeIndexJob)]
    assert result.status == "failed"
    assert result.segment_count == 0
    assert result.warnings == ["unsupported_or_unconfigured:media_transcription_provider"]
    assert documents[-1].status == "failed"
    assert documents[-1].doc_metadata_json["media_kind"] == "audio"
    assert documents[-1].doc_metadata_json["error"] == "unsupported_or_unconfigured"
    assert jobs[-1].stage == "transcribing"
    assert jobs[-1].status == "failed"
    assert jobs[-1].error_message == "unsupported_or_unconfigured:media_transcription_provider"


@pytest.mark.asyncio
async def test_ingest_audio_uses_transcription_provider_then_indexes_transcript(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    provider = _FakeMediaTranscriptionProvider()
    session = _FakeAsyncSession()
    service = PersonalKnowledgeService(
        data_root=tmp_path,
        extractor=_NoopKnowledgeExtractor(),
        media_provider=provider,
    )

    result = await service.ingest_source_bytes(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        filename="meeting.mp3",
        data=b"fake audio",
        title="Meeting recording",
        source_kind="upload",
        source_mime_type="audio/mpeg",
    )

    documents = [obj for obj in session.added if isinstance(obj, KnowledgeDocument)]
    segments = [obj for obj in session.added if isinstance(obj, KnowledgeSegment)]
    assert result.status == "ready"
    assert provider.calls[0]["media_kind"] == "audio"
    assert provider.calls[0]["tenant_id"] == tenant_id
    assert provider.calls[0]["owner_user_id"] == owner_id
    assert documents[-1].title == "Meeting recording"
    assert documents[-1].source_kind == "upload"
    assert documents[-1].doc_metadata_json["media_kind"] == "audio"
    assert documents[-1].doc_metadata_json["media_provider"] == "fake_media_provider"
    assert documents[-1].doc_metadata_json["media_duration_seconds"] == 12.5
    assert "Personal KB should keep source refs" in segments[-1].content


@pytest.mark.asyncio
async def test_process_import_jobs_consumes_queued_jobs_and_refuses_failed_reselect(tmp_path: Path) -> None:
    """Normal workers select queued jobs and stale-running leases only. Even
    when a failed row is hand-fed to the batch, it is refused — a failed job
    re-enters queued exclusively through the explicit retry CAS."""
    from app.services.personal_knowledge_service import PersonalKnowledgeIngestResult, PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    job_a = SimpleNamespace(id=uuid.uuid4(), document_id=uuid.uuid4(), status="queued")
    job_b = SimpleNamespace(id=uuid.uuid4(), document_id=uuid.uuid4(), status="failed")
    session = _QueuedSession([[(job_a,), (job_b,)]])

    class _BatchService(PersonalKnowledgeService):
        def __init__(self) -> None:
            super().__init__(data_root=tmp_path)
            self.rebuild_calls: list[uuid.UUID] = []

        async def rebuild_personal_document_index(self, session, **kwargs):
            self.rebuild_calls.append(kwargs["document_id"])
            return PersonalKnowledgeIngestResult(
                document_id=kwargs["document_id"],
                job_id=job_a.id,
                source_sha256="a" * 64,
                artifact_hash="a" * 64,
                canonical_md_path="a.md",
                segment_count=2,
                status="ready",
                warnings=[],
            )

    service = _BatchService()

    summary = await service.process_import_jobs(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        current_user_id=owner_id,
        limit=5,
    )

    # Only the queued job ran; the failed row was refused without a rebuild.
    assert service.rebuild_calls == [job_a.document_id]
    assert summary.succeeded == 1
    assert summary.failed == 0
    assert summary.skipped == 1
    assert summary.results[0]["status"] == "ready"
    assert summary.results[1]["status"] == "skipped"
    assert summary.results[1]["warnings"] == ["status_not_claimable"]


@pytest.mark.asyncio
async def test_process_import_jobs_consumes_queued_source_bytes_payload(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeIngestResult, PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    document_id = uuid.uuid4()
    job_id = uuid.uuid4()
    source_hash = "d" * 64
    queued_path = tmp_path / "persons" / str(owner_id) / "kb" / "imports" / "dd" / "queued-report.md"
    queued_path.parent.mkdir(parents=True)
    queued_path.write_bytes(b"# queued source")
    job = SimpleNamespace(
        id=job_id,
        document_id=document_id,
        status="queued",
        attempt_count=0,
        job_metadata_json={
            "queued_import_kind": "source_bytes",
            "queued_source_path": queued_path.relative_to(tmp_path).as_posix(),
            "source_filename": "queued-report.md",
            "source_kind": "upload",
            "source_uri": "upload://queued-report.md",
            "source_mime_type": "text/markdown",
            "title": "Queued report",
            "agent_searchable": True,
            "sensitivity": "internal",
            "created_by_user_id": str(owner_id),
            "doc_metadata": {"source_context": "unit-test"},
            "source_sha256": source_hash,
        },
    )
    session = _QueuedSession([[(job,)]])

    class _WorkerService(PersonalKnowledgeService):
        def __init__(self) -> None:
            super().__init__(data_root=tmp_path)
            self.ingest_calls: list[dict] = []

        async def ingest_source_bytes(self, session, **kwargs):
            self.ingest_calls.append(kwargs)
            return PersonalKnowledgeIngestResult(
                document_id=document_id,
                job_id=job_id,
                source_sha256=kwargs["source_sha256"],
                artifact_hash="e" * 64,
                canonical_md_path="persons/owner/kb/documents/dd.md",
                segment_count=1,
                status="ready",
                warnings=[],
            )

    service = _WorkerService()

    summary = await service.process_import_jobs(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        current_user_id=owner_id,
        limit=5,
    )

    assert summary.attempted == 1
    assert summary.succeeded == 1
    assert service.ingest_calls[0]["filename"] == "queued-report.md"
    assert service.ingest_calls[0]["data"] == b"# queued source"
    assert service.ingest_calls[0]["source_sha256"] == source_hash
    assert job.stage == "indexed"
    assert job.status == "ready"
    assert job.attempt_count == 1


@pytest.mark.asyncio
async def test_list_personal_graph_returns_entities_links_and_assertions(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    entity_a = KnowledgeEntity(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        scope_type="person",
        scope_id=owner_id,
        canonical_name="Open Notebook",
        entity_type="project",
        aliases_json=["OpenNotebook"],
        description="Notebook source workflow",
        confidence=0.9,
        source_refs_json=[{"document_id": str(uuid.uuid4()), "segment_id": str(uuid.uuid4())}],
    )
    entity_b = KnowledgeEntity(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        scope_type="person",
        scope_id=owner_id,
        canonical_name="Personal KB",
        entity_type="system",
        aliases_json=[],
        description=None,
        confidence=0.8,
        source_refs_json=[],
    )
    link = KnowledgeLink(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        scope_type="person",
        scope_id=owner_id,
        from_kind="entity",
        from_id=entity_a.id,
        to_kind="entity",
        to_id=entity_b.id,
        relation="inspires",
        confidence=0.75,
        source_refs_json=[{"segment_id": str(uuid.uuid4())}],
    )
    assertion = KnowledgeAssertion(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        scope_type="person",
        scope_id=owner_id,
        subject_text="Personal KB",
        predicate="needs",
        object_text="source refs",
        confidence=0.95,
        status="active",
        source_refs_json=[{"segment_id": str(uuid.uuid4())}],
    )
    session = _QueuedSession(
        [
            [(entity_a,), (entity_b,)],
            [(link,)],
            [(assertion,)],
        ]
    )
    service = PersonalKnowledgeService(data_root=tmp_path)

    graph = await service.list_personal_graph(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        current_user_id=owner_id,
        limit=20,
    )

    assert graph.entities[0].canonical_name == "Open Notebook"
    assert graph.entities[0].aliases == ["OpenNotebook"]
    assert graph.links[0].relation == "inspires"
    assert graph.links[0].from_id == entity_a.id
    assert graph.assertions[0].predicate == "needs"
    assert graph.assertions[0].object_text == "source refs"


@pytest.mark.asyncio
async def test_non_owner_personal_graph_returns_empty(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    session = _QueuedSession([[(SimpleNamespace(),)]])
    service = PersonalKnowledgeService(data_root=tmp_path)

    graph = await service.list_personal_graph(
        session,
        tenant_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        current_user_id=uuid.uuid4(),
        limit=20,
    )

    assert graph.entities == []
    assert graph.links == []
    assert graph.assertions == []
    assert session.executed == []


def test_personal_knowledge_access_predicate_filters_expired_grants() -> None:
    from app.services.personal_knowledge_service import build_personal_knowledge_search_statement

    statement = build_personal_knowledge_search_statement(
        tenant_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        query="source refs",
        principal=AgentRuntimePrincipal(
            agent_id=uuid.uuid4(),
            requester_user_id=uuid.uuid4(),
            session_id="expiry-check-session",
        ),
        limit=5,
    )
    compiled = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))

    assert "knowledge_grants.expires_at IS NULL" in compiled
    assert "knowledge_grants.expires_at > now()" in compiled


def test_human_browser_principal_never_inherits_agent_owner_authority() -> None:
    from app.services.personal_knowledge_access import HumanBrowserPrincipal
    from app.services.personal_knowledge_service import build_personal_knowledge_search_statement

    statement = build_personal_knowledge_search_statement(
        tenant_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        query="owner secret",
        principal=HumanBrowserPrincipal(user_id=uuid.uuid4()),
        limit=5,
    )
    compiled = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))

    assert "knowledge_grants" in compiled
    assert "knowledge_grants.grantee_type" in compiled
    assert "agents.id" not in compiled
    assert "AND knowledge_documents.agent_searchable IS true" not in compiled


def test_agent_runtime_principal_is_agent_searchable_and_delegation_bound() -> None:
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.services.personal_knowledge_service import build_personal_knowledge_search_statement

    requester_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    principal = AgentRuntimePrincipal(
        agent_id=agent_id,
        requester_user_id=requester_id,
        session_id="session-42",
        delegation_id="delegation-42",
        purpose="a2a_delegation",
    )
    statement = build_personal_knowledge_search_statement(
        tenant_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        query="delegated owner note",
        principal=principal,
        limit=5,
    )
    compiled = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))

    assert principal.evidence() == {
        "principal_type": "agent_runtime",
        "agent_id": str(agent_id),
        "requester_user_id": str(requester_id),
        "session_id": "session-42",
        "runtime_task_id": None,
        "delegation_id": "delegation-42",
        "purpose": "a2a_delegation",
        "autonomous": False,
    }
    assert "agents.id" not in compiled
    assert "knowledge_documents.agent_searchable IS true" in compiled
    assert "knowledge_grants" in compiled
    assert "knowledge_grants.delegation_id" in compiled


def test_personal_import_job_claim_statement_uses_skip_locked_and_time_guards() -> None:
    from app.services.personal_knowledge_service import build_personal_knowledge_job_claim_statement

    statement = build_personal_knowledge_job_claim_statement(
        tenant_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        statuses=("queued", "running"),
        queued_before=datetime.now(timezone.utc) - timedelta(seconds=30),
        running_before=datetime.now(timezone.utc) - timedelta(minutes=10),
        max_attempts=5,
        limit=10,
    )
    compiled = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))

    assert "knowledge_index_jobs.status = " in compiled
    assert "knowledge_index_jobs.updated_at <=" in compiled
    assert "knowledge_index_jobs.attempt_count <" in compiled
    assert "FOR UPDATE SKIP LOCKED" in compiled


@pytest.mark.asyncio
async def test_process_import_jobs_marks_attempt_limit_as_failed_without_rebuild(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    job = SimpleNamespace(
        id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        stage="queued",
        status="queued",
        error_message=None,
        attempt_count=5,
        job_metadata_json={},
    )
    session = _QueuedSession([[(job,)]])

    class _PoisonService(PersonalKnowledgeService):
        async def rebuild_personal_document_index(self, *_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("poisoned jobs must not rebuild")

    summary = await _PoisonService(data_root=tmp_path).process_import_jobs(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        current_user_id=owner_id,
        limit=5,
        max_attempts=5,
    )

    assert summary.attempted == 1
    assert summary.failed == 1
    assert job.stage == "failed"
    assert job.status == "failed"
    assert job.error_message == "personal_kb_import_attempt_limit_exceeded"
    assert job.job_metadata_json["warnings"] == ["personal_kb_import_attempt_limit_exceeded"]


@pytest.mark.asyncio
async def test_process_import_jobs_isolates_poison_job_from_rest_of_batch(tmp_path: Path) -> None:
    """A job that raises mid-processing must not starve the rest of the batch."""
    from app.services.personal_knowledge_service import PersonalKnowledgeIngestResult, PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    poison = SimpleNamespace(
        id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        stage="queued",
        status="queued",
        error_message=None,
        attempt_count=0,
        job_metadata_json={},
    )
    healthy = SimpleNamespace(
        id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        stage="queued",
        status="queued",
        error_message=None,
        attempt_count=0,
        job_metadata_json={},
    )
    session = _QueuedSession([[(poison,), (healthy,)]])

    class _PartiallyPoisonedService(PersonalKnowledgeService):
        def __init__(self) -> None:
            super().__init__(data_root=tmp_path)
            self.rebuild_calls: list[uuid.UUID] = []

        async def rebuild_personal_document_index(self, session, **kwargs):
            document_id = kwargs["document_id"]
            self.rebuild_calls.append(document_id)
            if document_id == poison.document_id:
                raise ValueError("markdown must not be empty")
            return PersonalKnowledgeIngestResult(
                document_id=document_id,
                job_id=healthy.id,
                source_sha256="c" * 64,
                artifact_hash="c" * 64,
                canonical_md_path="c.md",
                segment_count=3,
                status="ready",
                warnings=[],
            )

    service = _PartiallyPoisonedService()

    summary = await service.process_import_jobs(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        current_user_id=owner_id,
        limit=5,
    )

    # The poison job did not abort the loop: the healthy job was still attempted.
    assert service.rebuild_calls == [poison.document_id, healthy.document_id]
    assert summary.attempted == 2
    assert summary.succeeded == 1
    assert summary.failed == 1
    # Poison job carries a terminal failed status with a worker-error warning.
    assert poison.stage == "failed"
    assert poison.status == "failed"
    assert any("personal_kb_import_worker_error" in warning for warning in poison.job_metadata_json["warnings"])
    assert summary.results[0]["status"] == "failed"
    # Healthy job indexed successfully after the poison job failed.
    assert healthy.status == "ready"
    assert summary.results[1]["status"] == "ready"


# ---------------------------------------------------------------------------
# RC-01: durable lifecycle, idempotency, typed exits (failing-first additions)
# ---------------------------------------------------------------------------


class _ExistingPairSession(_FakeAsyncSession):
    """Returns the seeded existing document for the first Select and the
    seeded existing job for the second (mirrors _queue_import_job's lookups)."""

    def __init__(self, existing_document, existing_job) -> None:
        super().__init__(existing_document=existing_document)
        self.existing_job = existing_job
        self._select_index = 0

    async def execute(self, statement):
        if isinstance(statement, Select):
            self._select_index += 1
            if self._select_index == 1:
                return _ScalarResult(self.existing_document)
            if self._select_index == 2:
                return _ScalarResult(self.existing_job)
        return await super().execute(statement)


def _existing_document_row(status: str, *, archived: bool = False, segment_count: int = 0) -> KnowledgeDocument:
    metadata: dict = {"segment_count": segment_count}
    if archived:
        metadata["archived_from_status"] = "ready"
    return KnowledgeDocument(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        scope_type="person",
        scope_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        source_kind="upload",
        source_uri="upload://dup.pdf",
        source_sha256="a" * 64,
        artifact_hash="a" * 64,
        title="Existing duplicate document",
        status=status,
        sensitivity="internal",
        agent_searchable=True,
        canonical_md_path="a.md",
        canonical_md_sha256="b" * 64,
        doc_metadata_json=metadata,
        created_by_user_id=None,
    )


def _existing_job_row(status: str, attempt_count: int) -> KnowledgeIndexJob:
    return KnowledgeIndexJob(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        scope_type="person",
        scope_id=uuid.uuid4(),
        artifact_hash="a" * 64,
        stage="indexed" if status in {"ready", "degraded"} else status,
        status=status,
        error_message=None,
        attempt_count=attempt_count,
        job_metadata_json={},
    )


async def test_duplicate_source_returns_existing_state_without_reset(tmp_path: Path) -> None:
    """Reuploading identical bytes is idempotent: ready stays ready, archived
    stays archived, attempts stay stable — only explicit Retry/Restore/Rebuild
    reprocesses."""
    service = PersonalKnowledgeService(data_root=tmp_path)
    owner_id = uuid.uuid4()
    for document_status, job_status, attempts in (
        ("ready", "ready", 1),
        ("archived", "ready", 2),
        ("ready", "failed", 3),
    ):
        document = _existing_document_row(document_status, archived=document_status == "archived", segment_count=4)
        job = _existing_job_row(job_status, attempts)
        session = _ExistingPairSession(document, job)

        result = await service._queue_import_job(
            session,
            tenant_id=document.tenant_id,
            owner_user_id=document.scope_id,
            title="Duplicate upload",
            source_kind="upload",
            source_uri="upload://dup.pdf",
            source_sha256="a" * 64,
            artifact_hash="a" * 64,
            canonical_md_path="a.md",
            canonical_md_sha256="b" * 64,
            created_by_user_id=owner_id,
            agent_searchable=True,
            sensitivity="internal",
            metadata={},
        )

        assert result.status == document_status, (document_status, result.status)
        assert result.document_id == document.id
        assert result.job_id == job.id
        # The existing rows were not mutated back to queued.
        assert document.status == document_status
        assert job.status == job_status
        assert int(job.attempt_count or 0) == attempts


async def test_duplicate_source_at_max_attempts_stays_terminal_not_queued(tmp_path: Path) -> None:
    document = _existing_document_row("ready", segment_count=2)
    job = _existing_job_row("failed", DEFAULT_IMPORT_JOB_MAX_ATTEMPTS)
    session = _ExistingPairSession(document, job)
    service = PersonalKnowledgeService(data_root=tmp_path)

    result = await service._queue_import_job(
        session,
        tenant_id=document.tenant_id,
        owner_user_id=document.scope_id,
        title="Duplicate at max attempts",
        source_kind="upload",
        source_uri="upload://dup.pdf",
        source_sha256="a" * 64,
        artifact_hash="a" * 64,
        canonical_md_path="a.md",
        canonical_md_sha256="b" * 64,
        created_by_user_id=uuid.uuid4(),
        agent_searchable=True,
        sensitivity="internal",
        metadata={},
    )

    assert result.status == "ready"
    assert job.status == "failed"
    assert int(job.attempt_count or 0) == DEFAULT_IMPORT_JOB_MAX_ATTEMPTS


class _SlowConverter:
    def convert_bytes(self, **kwargs):
        import time

        time.sleep(0.3)
        return SimpleNamespace(
            markdown="# slow",
            warnings=[],
            engine="slow-test",
            artifact_markdown_path="",
            artifact_metadata_path="",
            source_mime_type="application/pdf",
            source_sha256="c" * 64,
        )


async def test_conversion_timeout_is_typed_retryable_failure(tmp_path: Path) -> None:
    """A physically slow converter becomes a typed retryable failure; the
    source evidence stays recorded and semantic content is not truncated."""
    session = _FakeAsyncSession()
    service = PersonalKnowledgeService(
        data_root=tmp_path,
        conversion_service=_SlowConverter(),
        conversion_timeout_seconds=0.05,
    )

    result = await service.ingest_source_bytes(
        session,
        tenant_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        filename="slow.pdf",
        data=b"%PDF-1.4 fake bytes for timeout regression",
        title="Slow PDF",
        source_kind="upload",
        source_uri="upload://slow.pdf",
        created_by_user_id=None,
        agent_searchable=True,
        sensitivity="internal",
        source_mime_type="application/pdf",
    )

    assert result.status == "failed"
    assert "conversion_timeout" in (result.warnings or [])
    failed_job = next(row for row in session.added if isinstance(row, KnowledgeIndexJob))
    assert failed_job.status == "failed"
    assert failed_job.error_message == "conversion_timeout"
    assert failed_job.job_metadata_json.get("error") == "conversion_timeout"
    assert failed_job.job_metadata_json.get("retryable") is True
    assert failed_job.job_metadata_json.get("source_filename") == "slow.pdf"


@pytest.mark.asyncio
async def test_default_conversion_path_uses_killable_process_seam(monkeypatch, tmp_path: Path) -> None:
    """Default production construction routes conversion through the shared
    killable process seam; the thread path is only reachable via test DI."""
    import app.services.document_conversion as document_conversion

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    seam_calls: list[dict] = []

    async def _fake_seam(**kwargs):
        seam_calls.append(kwargs)
        return document_conversion.DocumentConversionResult(
            markdown="# Wired\n\nProcess seam body.",
            plain_text="Wired Process seam body.",
            source_path="",
            source_uri=kwargs["source_uri"],
            source_sha256="c" * 64,
            source_mime_type=kwargs["source_mime_type"] or "text/markdown",
            engine="killable-process-test",
            used_ocr=False,
            used_vision=False,
            page_count=1,
            artifact_markdown_path="",
            artifact_metadata_path="",
            warnings=(),
        )

    def _forbidden_to_thread(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("default production path must not use asyncio.to_thread")

    monkeypatch.setattr(document_conversion, "convert_bytes_in_killable_process", _fake_seam)
    monkeypatch.setattr(asyncio, "to_thread", _forbidden_to_thread)

    session = _FakeAsyncSession()
    service = PersonalKnowledgeService(
        data_root=tmp_path,
        extractor=_NoopKnowledgeExtractor(),
        conversion_timeout_seconds=12.5,
    )

    result = await service.ingest_source_bytes(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        filename="wired.md",
        data=b"# raw wired body",
        source_kind="upload",
        source_uri="upload://wired.md",
        created_by_user_id=owner_id,
        agent_searchable=True,
        sensitivity="internal",
        source_mime_type="text/markdown",
    )

    assert result.status == "ready"
    assert seam_calls == [
        {
            "data": b"# raw wired body",
            "filename": "wired.md",
            "workspace_root": tmp_path / "persons" / str(owner_id) / "kb",
            "timeout_seconds": 12.5,
            "source_uri": "upload://wired.md",
            "source_mime_type": "text/markdown",
            "tenant_id": tenant_id,
            "agent_id": None,
            "user_id": owner_id,
            "mode": "auto",
            "force_refresh": False,
        }
    ]


@pytest.mark.asyncio
async def test_default_conversion_path_seam_timeout_stays_typed_retryable(monkeypatch, tmp_path: Path) -> None:
    """A TimeoutError from the killable seam keeps the typed retryable conversion_timeout contract."""
    import app.services.document_conversion as document_conversion

    async def _timeout_seam(**kwargs):
        raise TimeoutError("physical kill")

    monkeypatch.setattr(document_conversion, "convert_bytes_in_killable_process", _timeout_seam)

    session = _FakeAsyncSession()
    service = PersonalKnowledgeService(data_root=tmp_path, conversion_timeout_seconds=0.05)

    result = await service.ingest_source_bytes(
        session,
        tenant_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        filename="slow.pdf",
        data=b"%PDF-1.4 fake bytes for seam timeout wiring",
        title="Slow PDF",
        source_kind="upload",
        source_uri="upload://slow.pdf",
        created_by_user_id=None,
        agent_searchable=True,
        sensitivity="internal",
        source_mime_type="application/pdf",
    )

    assert result.status == "failed"
    assert "conversion_timeout" in (result.warnings or [])
    failed_job = next(row for row in session.added if isinstance(row, KnowledgeIndexJob))
    assert failed_job.status == "failed"
    assert failed_job.error_message == "conversion_timeout"
    assert failed_job.job_metadata_json.get("error") == "conversion_timeout"
    assert failed_job.job_metadata_json.get("retryable") is True


class _JobLookupSession(_FakeAsyncSession):
    """Returns a specific existing job row for Select statements."""

    def __init__(self, job) -> None:
        super().__init__()
        self.job = job
        self.updates: list[Update] = []

    async def execute(self, statement):
        if isinstance(statement, Select):
            return _ScalarResult(self.job)
        if isinstance(statement, Update):
            self.updates.append(statement)
            return SimpleNamespace(rowcount=1, scalar_one_or_none=lambda: None)
        return await super().execute(statement)


class _SegmentCountSession(_JobLookupSession):
    async def execute(self, statement):
        if isinstance(statement, Select):
            compiled = str(statement.compile(compile_kwargs={"literal_binds": False}))
            if "knowledge_segments" in compiled:
                return _ScalarResult(3)
            return _ScalarResult(self.job)
        return await super().execute(statement)


async def test_cancel_queued_import_job_is_cas_guarded(tmp_path: Path) -> None:
    job = _existing_job_row("queued", 0)
    session = _JobLookupSession(job)
    service = PersonalKnowledgeService(data_root=tmp_path)
    owner = uuid.uuid4()

    summary = await service.cancel_import_job(
        session, tenant_id=job.tenant_id, owner_user_id=owner, job_id=job.id, current_user_id=owner
    )

    assert summary is not None
    assert summary.status == "cancelled"
    assert summary.terminal is True
    assert summary.cancellable is False
    # The cancel must be compare-and-set guarded on status == 'queued'.
    cancel_update = next(statement for statement in session.updates if isinstance(statement, Update))
    compiled = str(cancel_update.compile(compile_kwargs={"literal_binds": False}))
    assert "status" in compiled


async def test_cancel_running_import_job_is_typed_not_cancellable(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeJobConflict

    job = _existing_job_row("running", 1)
    session = _JobLookupSession(job)
    service = PersonalKnowledgeService(data_root=tmp_path)
    owner = uuid.uuid4()

    with pytest.raises(PersonalKnowledgeJobConflict) as conflict:
        await service.cancel_import_job(
            session, tenant_id=job.tenant_id, owner_user_id=owner, job_id=job.id, current_user_id=owner
        )
    assert conflict.value.code == "not_cancellable_while_running"
    assert conflict.value.cancellable is False


async def test_retry_import_job_requeues_without_running_conversion(tmp_path: Path) -> None:
    job = _existing_job_row("failed", 1)
    session = _JobLookupSession(job)
    service = PersonalKnowledgeService(data_root=tmp_path)
    owner = uuid.uuid4()

    conversions = []

    async def _fail_if_called(*args, **kwargs):  # pragma: no cover - must not run
        conversions.append(True)
        raise AssertionError("retry must not run conversion inside the request")

    service.ingest_markdown = _fail_if_called  # type: ignore[method-assign]
    service.rebuild_personal_document_index = _fail_if_called  # type: ignore[method-assign]

    summary = await service.retry_import_job(
        session, tenant_id=job.tenant_id, owner_user_id=owner, job_id=job.id, current_user_id=owner
    )

    assert summary is not None
    assert summary.status == "queued"
    assert summary.terminal is False
    # A queued job is not retryable again — the Retry affordance is owned by
    # failed jobs under the attempt ceiling; the claim increment owns attempts.
    assert summary.retryable is False
    assert summary.attempt_count == 1
    assert conversions == []


async def test_job_summary_derives_lifecycle_view_fields(tmp_path: Path) -> None:
    """The read model derives terminal/retryable/cancellable/error_code and
    exposes max_attempts without any schema change."""
    from app.services.personal_knowledge_service import _job_lifecycle_view

    queued = _existing_job_row("queued", 0)
    running = _existing_job_row("running", 1)
    failed_retryable = _existing_job_row("failed", 1)
    failed_retryable.job_metadata_json = {"error": "conversion_timeout"}
    ready = _existing_job_row("ready", 1)

    assert _job_lifecycle_view(queued).terminal is False
    assert _job_lifecycle_view(queued).cancellable is True
    assert _job_lifecycle_view(running).terminal is False
    assert _job_lifecycle_view(running).cancellable is False
    retryable_view = _job_lifecycle_view(failed_retryable)
    assert retryable_view.terminal is True
    assert retryable_view.retryable is True
    assert retryable_view.error_code == "conversion_timeout"
    assert _job_lifecycle_view(ready).terminal is True
    assert _job_lifecycle_view(ready).retryable is False


async def test_patch_personal_document_status_accepts_only_archive(tmp_path: Path) -> None:
    document = _existing_document_row("ready", segment_count=3)

    class _DocumentOnlySession(_FakeAsyncSession):
        async def execute(self, statement):
            if isinstance(statement, Select):
                return _ScalarResult(document)
            return await super().execute(statement)

    session = _DocumentOnlySession()
    service = PersonalKnowledgeService(data_root=tmp_path)

    with pytest.raises(ValueError) as rejection:
        await service.patch_personal_document(
            session,
            tenant_id=document.tenant_id,
            owner_user_id=document.scope_id,
            document_id=document.id,
            current_user_id=document.scope_id,
            agent_id=None,
            status="ready",
        )
    assert "unsupported_status" in str(rejection.value)

    archived = await service.patch_personal_document(
        session,
        tenant_id=document.tenant_id,
        owner_user_id=document.scope_id,
        document_id=document.id,
        current_user_id=document.scope_id,
        agent_id=None,
        status="archived",
    )
    assert archived is not None
    assert document.status == "archived"
    assert document.doc_metadata_json["archived_from_status"] == "ready"


async def test_restore_personal_document_recovers_previous_consumable_state(tmp_path: Path) -> None:
    document = _existing_document_row("archived", archived=True, segment_count=3)

    class _DocumentRoutingSession(_FakeAsyncSession):
        async def execute(self, statement):
            if isinstance(statement, Select):
                compiled = str(statement.compile(compile_kwargs={"literal_binds": False}))
                if "knowledge_segments" in compiled:
                    return SimpleNamespace(scalar_one=lambda: 3, scalar_one_or_none=lambda: 3)
                return _ScalarResult(document)
            return await super().execute(statement)

    session = _DocumentRoutingSession()
    service = PersonalKnowledgeService(data_root=tmp_path)

    restored = await service.restore_personal_document(
        session,
        tenant_id=document.tenant_id,
        owner_user_id=document.scope_id,
        document_id=document.id,
        current_user_id=document.scope_id,
    )

    assert restored is not None
    assert restored.status == "ready"
    assert document.doc_metadata_json.get("archived_from_status") is None


async def test_restore_never_invents_ready_without_consumable_state(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeDocumentConflict

    document = _existing_document_row("archived")
    document.doc_metadata_json = {}  # no archived_from_status recorded

    class _NoSegmentsSession(_FakeAsyncSession):
        async def execute(self, statement):
            if isinstance(statement, Select):
                compiled = str(statement.compile(compile_kwargs={"literal_binds": False}))
                if "knowledge_segments" in compiled:
                    return SimpleNamespace(scalar_one=lambda: 0, scalar_one_or_none=lambda: 0)
                return _ScalarResult(document)
            return await super().execute(statement)

    session = _NoSegmentsSession()
    service = PersonalKnowledgeService(data_root=tmp_path)

    with pytest.raises(PersonalKnowledgeDocumentConflict) as conflict:
        await service.restore_personal_document(
            session,
            tenant_id=document.tenant_id,
            owner_user_id=document.scope_id,
            document_id=document.id,
            current_user_id=document.scope_id,
        )
    assert conflict.value.code == "restore_no_consumable_state"


# ---------------------------------------------------------------------------
# RC-01 corrections: lifecycle/result split, typed error taxonomy, timestamps
# (failing-first additions)
# ---------------------------------------------------------------------------


async def test_job_summary_splits_lifecycle_and_result_status(tmp_path: Path) -> None:
    """B5: lifecycle_status (queued/running/completed/failed/cancelled) is
    distinct from result_status (ready/degraded/failed/cancelled); raw status
    stays only for compatibility."""
    from app.services.personal_knowledge_service import _job_lifecycle_view

    queued = _job_lifecycle_view(_existing_job_row("queued", 0))
    assert queued.lifecycle_status == "queued"
    assert queued.result_status is None
    assert queued.terminal is False

    running = _job_lifecycle_view(_existing_job_row("running", 1))
    assert running.lifecycle_status == "running"
    assert running.result_status is None
    assert running.terminal is False

    ready = _job_lifecycle_view(_existing_job_row("ready", 1))
    assert ready.lifecycle_status == "completed"
    assert ready.result_status == "ready"
    assert ready.terminal is True

    degraded = _job_lifecycle_view(_existing_job_row("degraded", 1))
    assert degraded.lifecycle_status == "completed"
    assert degraded.result_status == "degraded"
    assert degraded.terminal is True

    failed = _job_lifecycle_view(_existing_job_row("failed", 1))
    assert failed.lifecycle_status == "failed"
    assert failed.result_status == "failed"
    assert failed.terminal is True

    cancelled = _existing_job_row("cancelled", 0)
    cancelled.job_metadata_json = {"cancelled_at": "2026-08-25T01:02:03+00:00"}
    cancelled_view = _job_lifecycle_view(cancelled)
    assert cancelled_view.lifecycle_status == "cancelled"
    assert cancelled_view.result_status == "cancelled"
    assert cancelled_view.terminal is True
    assert cancelled_view.cancelled_at == "2026-08-25T01:02:03+00:00"


def _queued_kind_job(tmp_path: Path, kind: str = "markdown") -> KnowledgeIndexJob:
    spool = tmp_path / "spool" / "queued.md"
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_text("# queued\n\nbody", encoding="utf-8")
    job = _existing_job_row("running", 1)
    job.job_metadata_json = {
        "queued_import_kind": kind,
        "queued_markdown_path": spool.relative_to(tmp_path).as_posix(),
        "title": "Queued doc",
        "source_kind": "paste",
        "source_uri": None,
        "created_by_user_id": None,
        "agent_searchable": True,
        "sensitivity": "internal",
        "doc_metadata": {},
        "source_sha256": "a" * 64,
        "claimed_token": "token-a",
    }
    return job


async def test_unknown_worker_error_maps_to_generic_code_not_raw_prose(tmp_path: Path) -> None:
    """B7: an unexpected exception — even one whose prose happens to contain
    format words like 'pdf' — maps to the one generic safe code. Raw exception
    text never becomes the user-facing error state."""
    job = _queued_kind_job(tmp_path)
    session = _JobLookupSession(job)
    service = PersonalKnowledgeService(data_root=tmp_path)
    owner = job.scope_id

    async def explode(*args, **kwargs):
        raise RuntimeError("vendor parser choked on pdf stream bytes at offset 41")

    service.ingest_markdown = explode  # type: ignore[method-assign]
    result = await service._process_queued_import_job(
        session,
        job=job,
        tenant_id=job.tenant_id,
        owner_user_id=owner,
        current_user_id=owner,
        claimed=True,
    )

    assert result is not None and result.status == "failed"
    assert job.status == "failed"
    assert job.error_message == "import_failed"
    assert job.job_metadata_json.get("error") == "import_failed"
    # Raw exception prose is not the contract; only the class name stays as
    # operator evidence.
    assert "vendor parser" not in str(job.error_message)
    assert "vendor parser" not in str(job.job_metadata_json.get("error"))
    assert job.job_metadata_json.get("failure_exception") == "RuntimeError"


async def test_converter_exception_maps_to_typed_conversion_failed(tmp_path: Path) -> None:
    """B7: any failure from the conversion boundary is the exact typed
    conversion_failed code — never inferred from message substrings."""
    job = _queued_kind_job(tmp_path, kind="source_bytes")
    spool = tmp_path / "spool" / "queued.pdf"
    spool.write_bytes(b"%PDF-1.4 bytes")
    job.job_metadata_json["queued_source_path"] = spool.relative_to(tmp_path).as_posix()
    job.job_metadata_json["source_filename"] = "queued.pdf"
    session = _JobLookupSession(job)
    service = PersonalKnowledgeService(data_root=tmp_path)

    class _ExplodingConverter:
        def convert_bytes(self, **kwargs):
            raise RuntimeError("totally unexpected converter internals")

    service.conversion_service = _ExplodingConverter()
    result = await service._process_queued_import_job(
        session,
        job=job,
        tenant_id=job.tenant_id,
        owner_user_id=job.scope_id,
        current_user_id=job.scope_id,
        claimed=True,
    )

    assert result is not None and result.status == "failed"
    assert job.job_metadata_json.get("error") == "conversion_failed"
    assert job.error_message == "conversion_failed"


async def test_cancel_returns_the_committed_cancelled_at(tmp_path: Path) -> None:
    """B8: the cancel response carries the exact cancelled_at that the CAS
    committed — one timestamp, visible on the summary and the metadata."""
    job = _existing_job_row("queued", 0)
    session = _JobLookupSession(job)
    service = PersonalKnowledgeService(data_root=tmp_path)
    owner = uuid.uuid4()

    summary = await service.cancel_import_job(
        session, tenant_id=job.tenant_id, owner_user_id=owner, job_id=job.id, current_user_id=owner
    )

    assert summary is not None
    committed = str(job.job_metadata_json.get("cancelled_at") or "")
    assert committed
    assert summary.cancelled_at == committed
    cancel_update = next(statement for statement in session.updates if isinstance(statement, Update))
    params = cancel_update.compile().params
    metadata_values = [value for value in params.values() if isinstance(value, dict)]
    assert any(value.get("cancelled_at") == committed for value in metadata_values)


async def test_retry_uses_one_consistent_retried_at(tmp_path: Path) -> None:
    """B8: retry stamps one retried_at and the returned summary reflects the
    same committed metadata (no double now() divergence)."""
    job = _existing_job_row("failed", 1)
    session = _JobLookupSession(job)
    service = PersonalKnowledgeService(data_root=tmp_path)
    owner = uuid.uuid4()

    summary = await service.retry_import_job(
        session, tenant_id=job.tenant_id, owner_user_id=owner, job_id=job.id, current_user_id=owner
    )

    assert summary is not None
    committed = str(job.job_metadata_json.get("retried_at") or "")
    assert committed
    retry_update = next(statement for statement in session.updates if isinstance(statement, Update))
    params = retry_update.compile().params
    metadata_values = [value for value in params.values() if isinstance(value, dict) and "retried_at" in value]
    # Exactly one stamped value, and it is the one the summary reflects.
    assert len(metadata_values) == 1
    assert metadata_values[0]["retried_at"] == committed


def test_claim_statement_never_selects_failed_jobs() -> None:
    """B4: the sole claim query admits queued and stale-running leases only;
    'failed' is not a selectable status even if a caller asks for it."""
    from app.services.personal_knowledge_jobs import build_personal_knowledge_job_claim_statement

    statement = build_personal_knowledge_job_claim_statement(
        tenant_id=None,
        owner_user_id=None,
        statuses=("failed",),
        queued_before=None,
        running_before=None,
        max_attempts=5,
        limit=10,
    )
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "'failed'" not in compiled


async def test_retry_refuses_requeue_at_or_above_attempt_ceiling(tmp_path: Path) -> None:
    """B1 guard: explicit retry must refuse to requeue a failed job that is
    already at the attempt ceiling (typed conflict, no queued resurrection)."""
    from app.services.personal_knowledge_service import PersonalKnowledgeJobConflict

    job = _existing_job_row("failed", DEFAULT_IMPORT_JOB_MAX_ATTEMPTS)
    session = _JobLookupSession(job)
    service = PersonalKnowledgeService(data_root=tmp_path)
    owner = uuid.uuid4()

    with pytest.raises(PersonalKnowledgeJobConflict) as conflict:
        await service.retry_import_job(
            session, tenant_id=job.tenant_id, owner_user_id=owner, job_id=job.id, current_user_id=owner
        )
    assert conflict.value.code == "retry_attempt_limit"
    assert conflict.value.retryable is False
    assert job.status == "failed"


async def test_duplicate_source_with_missing_job_row_returns_null_job_id(tmp_path: Path) -> None:
    """Codex-2: a deduped upload whose persisted document has no matching job
    row returns job_id=None — never a fabricated id for nonexistent evidence."""
    document = _existing_document_row("ready", segment_count=2)
    session = _ExistingPairSession(document, None)
    service = PersonalKnowledgeService(data_root=tmp_path)

    result = await service._queue_import_job(
        session,
        tenant_id=document.tenant_id,
        owner_user_id=document.scope_id,
        title="Duplicate with missing job",
        source_kind="upload",
        source_uri="upload://dup.pdf",
        source_sha256="a" * 64,
        artifact_hash="a" * 64,
        canonical_md_path="a.md",
        canonical_md_sha256="b" * 64,
        created_by_user_id=uuid.uuid4(),
        agent_searchable=True,
        sensitivity="internal",
        metadata={},
    )

    assert result.document_id == document.id
    assert result.job_id is None
    assert result.status == "ready"


async def test_retryable_contract_failed_cancelled_and_permanent_codes(tmp_path: Path) -> None:
    """One retryability authority: cancelled requeues while attempts remain;
    permanent codes are non-retryable; transient failures stay retryable."""
    from app.services.personal_knowledge_service import PersonalKnowledgeJobConflict, _job_lifecycle_view

    cancelled = _existing_job_row("cancelled", 1)
    cancelled_view = _job_lifecycle_view(cancelled)
    assert cancelled_view.retryable is True

    # Evidence-missing codes are recoverable by re-uploading the same bytes:
    # the spool/artifact is rewritten on queue, so an explicit retry succeeds.
    for recoverable_code in (
        "conversion_timeout",
        "conversion_failed",
        "source_missing",
        "canonical_markdown_missing",
        "worker_error",
        "import_failed",
    ):
        recoverable = _existing_job_row("failed", 1)
        recoverable.job_metadata_json = {"error": recoverable_code}
        assert _job_lifecycle_view(recoverable).retryable is True, recoverable_code

    # Truly permanent codes: input type/structure, orphaned object, attempt ceiling.
    for permanent_code in (
        "unsupported_file_type",
        "document_missing",
        "import_payload_invalid",
        "personal_kb_import_attempt_limit_exceeded",
    ):
        permanent = _existing_job_row("failed", 1)
        permanent.job_metadata_json = {"error": permanent_code}
        assert _job_lifecycle_view(permanent).retryable is False, permanent_code

        session = _JobLookupSession(permanent)
        service = PersonalKnowledgeService(data_root=tmp_path)
        owner = uuid.uuid4()
        with pytest.raises(PersonalKnowledgeJobConflict) as conflict:
            await service.retry_import_job(
                session,
                tenant_id=permanent.tenant_id,
                owner_user_id=owner,
                job_id=permanent.id,
                current_user_id=owner,
            )
        assert conflict.value.code == "not_retryable", permanent_code
        assert permanent.status == "failed"

    # Cancelled under the ceiling requeues through the same helper.
    cancelled_session = _JobLookupSession(cancelled)
    service = PersonalKnowledgeService(data_root=tmp_path)
    owner = uuid.uuid4()
    summary = await service.retry_import_job(
        cancelled_session,
        tenant_id=cancelled.tenant_id,
        owner_user_id=owner,
        job_id=cancelled.id,
        current_user_id=owner,
    )
    assert summary is not None
    assert summary.lifecycle_status == "queued"
