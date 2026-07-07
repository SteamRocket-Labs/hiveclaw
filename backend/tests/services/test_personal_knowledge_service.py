from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Delete, Update
from sqlalchemy.sql.selectable import Select

from app.models.knowledge import KnowledgeDocument, KnowledgeIndexJob, KnowledgeSegment


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
        self.executed: list[object] = []
        self.flush_count = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_count += 1

    async def execute(self, statement):
        self.executed.append(statement)
        if isinstance(statement, Select):
            return _ScalarResult(self.existing_document)
        return _ScalarResult()


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

    async def execute(self, statement):
        self.executed.append(statement)
        if not self.results:
            return _RowsResult([])
        return _RowsResult(self.results.pop(0))


def test_personal_knowledge_artifact_path_is_person_scope_stable(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import personal_knowledge_artifact_path

    owner_id = uuid.uuid4()
    source_hash = "a" * 64

    path = personal_knowledge_artifact_path(tmp_path, owner_id, source_hash)

    assert path == tmp_path / "persons" / str(owner_id) / "kb" / "documents" / "aa" / f"{source_hash}.md"


def test_segment_markdown_preserves_heading_path_and_stable_hashes() -> None:
    from app.services.personal_knowledge_service import segment_markdown

    markdown = "# Root\n\nIntro paragraph.\n\n## Alpha\n\n" + ("alpha detail " * 80) + "\n\n## Beta\n\nbeta detail"

    first = segment_markdown(markdown, max_segment_chars=220, overlap_chars=30)
    second = segment_markdown(markdown, max_segment_chars=220, overlap_chars=30)

    assert [segment.segment_hash for segment in first] == [segment.segment_hash for segment in second]
    assert any(segment.heading_path == ["Root", "Alpha"] for segment in first)
    assert any("alpha detail" in segment.content for segment in first)
    assert all(segment.content.strip() for segment in first)


@pytest.mark.asyncio
async def test_ingest_markdown_writes_artifact_document_segments_and_index_job(tmp_path: Path) -> None:
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    user_id = owner_id
    session = _FakeAsyncSession()
    service = PersonalKnowledgeService(data_root=tmp_path)

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


def test_owner_search_statement_uses_person_scope_without_grant_requirement() -> None:
    from app.services.personal_knowledge_service import build_personal_knowledge_search_statement

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()

    statement = build_personal_knowledge_search_statement(
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        query="source refs",
        current_user_id=owner_id,
        agent_id=uuid.uuid4(),
        limit=5,
    )
    compiled = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))

    assert "knowledge_documents.scope_type" in compiled
    assert "knowledge_documents.scope_id" in compiled
    assert "knowledge_segments.tsv @@ plainto_tsquery" in compiled
    assert "knowledge_grants" not in compiled


def test_external_search_statement_requires_matching_user_or_agent_grant() -> None:
    from app.services.personal_knowledge_service import build_personal_knowledge_search_statement

    statement = build_personal_knowledge_search_statement(
        tenant_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        query="source refs",
        current_user_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        limit=5,
    )
    compiled = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))

    assert "knowledge_grants" in compiled
    assert "knowledge_grants.grantee_type" in compiled
    assert "knowledge_grants.resource_type" in compiled
    assert "knowledge_grants.permission IN" in compiled


def test_personal_document_list_statement_requires_grant_for_non_owner() -> None:
    from app.services.personal_knowledge_service import build_personal_knowledge_document_list_statement

    statement = build_personal_knowledge_document_list_statement(
        tenant_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        current_user_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        limit=25,
    )
    compiled = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))

    assert "knowledge_documents.scope_type" in compiled
    assert "knowledge_documents.scope_id" in compiled
    assert "knowledge_grants" in compiled
    assert "knowledge_grants.permission IN" in compiled


def test_personal_document_list_statement_does_not_require_grant_for_owner() -> None:
    from app.services.personal_knowledge_service import build_personal_knowledge_document_list_statement

    owner_id = uuid.uuid4()
    statement = build_personal_knowledge_document_list_statement(
        tenant_id=uuid.uuid4(),
        owner_user_id=owner_id,
        current_user_id=owner_id,
        agent_id=uuid.uuid4(),
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
        current_user_id=owner_id,
        agent_id=uuid.uuid4(),
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
        current_user_id=owner_id,
        agent_id=uuid.uuid4(),
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
        current_user_id=owner_id,
        agent_id=uuid.uuid4(),
        limit=3,
    )

    assert len(hits) == 1
    assert hits[0].document_id == document.id
    assert hits[0].segment_id == segment.id
    assert hits[0].title == "Retrieval notes"
    assert hits[0].source_ref == f"kb://person/{owner_id}/documents/{document.id}#segment={segment.id}"
    assert hits[0].score == pytest.approx(0.87)
    assert session.executed
