"""Personal Knowledge Base ingestion and search primitives."""

from __future__ import annotations

import hashlib
import mimetypes
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import and_, delete, desc, exists, false, func, or_, select, true, update

from app.config import get_settings
from app.models.knowledge import KnowledgeDocument, KnowledgeGrant, KnowledgeIndexJob, KnowledgeSegment


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_WHITESPACE_RE = re.compile(r"\s+")
_SUPPORTED_IMPORT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".html",
    ".htm",
    ".csv",
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
}


@dataclass(frozen=True)
class KnowledgeSegmentDraft:
    position: int
    heading_path: list[str]
    content: str
    segment_hash: str
    token_count: int


@dataclass(frozen=True)
class PersonalKnowledgeIngestResult:
    document_id: uuid.UUID
    job_id: uuid.UUID | None
    source_sha256: str
    artifact_hash: str
    canonical_md_path: str
    segment_count: int
    status: str
    warnings: list[str]


@dataclass(frozen=True)
class PersonalKnowledgeDocumentSummary:
    document_id: uuid.UUID
    title: str
    source_kind: str
    source_uri: str | None
    source_sha256: str
    source_ref: str
    canonical_md_path: str
    status: str
    sensitivity: str
    agent_searchable: bool
    segment_count: int
    created_at: Any
    updated_at: Any
    metadata: dict[str, Any]


@dataclass(frozen=True)
class PersonalKnowledgeDocumentSegment:
    segment_id: uuid.UUID
    position: int
    heading_path: list[str]
    content: str
    token_count: int


@dataclass(frozen=True)
class PersonalKnowledgeDocumentDetail(PersonalKnowledgeDocumentSummary):
    segments: list[PersonalKnowledgeDocumentSegment]


@dataclass(frozen=True)
class KnowledgeSearchHit:
    document_id: uuid.UUID
    segment_id: uuid.UUID
    title: str
    snippet: str
    source_ref: str
    score: float
    heading_path: list[str]
    sensitivity: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class PersonalKnowledgeJobSummary:
    job_id: uuid.UUID
    document_id: uuid.UUID
    stage: str
    status: str
    artifact_hash: str
    error_message: str | None
    attempt_count: int
    metadata: dict[str, Any]
    created_at: Any
    updated_at: Any


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalize_markdown(markdown: str) -> str:
    clean = str(markdown or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return f"{clean}\n" if clean else ""


def _clean_title(title: str) -> str:
    clean = _WHITESPACE_RE.sub(" ", str(title or "").strip())
    return clean or "Untitled knowledge document"


def _rough_token_count(text: str) -> int:
    return max(1, len(_WHITESPACE_RE.findall(text)) + 1) if text.strip() else 0


def personal_knowledge_artifact_path(data_root: str | Path, owner_user_id: uuid.UUID, source_sha256: str) -> Path:
    """Return the canonical Markdown artifact path for one person-scope source."""

    source_hash = str(source_sha256).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ValueError("source_sha256 must be a 64-character lowercase hex digest")
    return Path(data_root) / "persons" / str(owner_user_id) / "kb" / "documents" / source_hash[:2] / f"{source_hash}.md"


def _personal_knowledge_root(data_root: str | Path, owner_user_id: uuid.UUID) -> Path:
    return Path(data_root) / "persons" / str(owner_user_id) / "kb"


def _safe_filename(filename: str) -> str:
    normalized = str(filename or "").replace("\\", "/")
    safe_name = Path(normalized).name.strip()
    if safe_name in {"", ".", ".."}:
        raise ValueError("filename is required")
    return safe_name


def _extension_for_filename(filename: str) -> str:
    return Path(filename).suffix.lower()


def _title_from_url(url: str) -> str:
    path_name = Path(urlparse(url).path).name
    if path_name:
        return path_name
    return urlparse(url).netloc or "Imported URL"


def title_from_filename_or_uri(filename: str, source_uri: str | None, explicit_title: str | None = None) -> str:
    if explicit_title and str(explicit_title).strip():
        return _clean_title(explicit_title)
    safe_name = _safe_filename(filename)
    if safe_name:
        return _clean_title(Path(safe_name).stem or safe_name)
    if source_uri:
        return _clean_title(_title_from_url(source_uri))
    return "Imported knowledge source"


def _escape_like(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _personal_knowledge_access_predicate(
    *,
    tenant_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    current_user_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
):
    if current_user_id == owner_user_id:
        return true()

    grantee_predicates = []
    if current_user_id is not None:
        grantee_predicates.append(
            and_(KnowledgeGrant.grantee_type == "user", KnowledgeGrant.grantee_id == current_user_id)
        )
    if agent_id is not None:
        grantee_predicates.append(and_(KnowledgeGrant.grantee_type == "agent", KnowledgeGrant.grantee_id == agent_id))
    if not grantee_predicates:
        return false()

    return exists(
        select(1).where(
            KnowledgeGrant.tenant_id == tenant_id,
            KnowledgeGrant.scope_type == "person",
            KnowledgeGrant.scope_id == owner_user_id,
            KnowledgeGrant.permission.in_(("read", "search", "manage")),
            or_(*grantee_predicates),
            or_(
                and_(KnowledgeGrant.resource_type == "scope", KnowledgeGrant.resource_id == owner_user_id),
                and_(KnowledgeGrant.resource_type == "document", KnowledgeGrant.resource_id == KnowledgeDocument.id),
                KnowledgeGrant.document_id == KnowledgeDocument.id,
            ),
            or_(KnowledgeGrant.expires_at.is_(None), KnowledgeGrant.expires_at > func.now()),
        )
    )


def build_personal_knowledge_document_list_statement(
    *,
    tenant_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    current_user_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    limit: int,
    document_id: uuid.UUID | None = None,
):
    segment_count = (
        select(func.count(KnowledgeSegment.id))
        .where(
            KnowledgeSegment.tenant_id == tenant_id,
            KnowledgeSegment.document_id == KnowledgeDocument.id,
            KnowledgeSegment.scope_type == "person",
            KnowledgeSegment.scope_id == owner_user_id,
        )
        .correlate(KnowledgeDocument)
        .scalar_subquery()
        .label("segment_count")
    )
    statement = (
        select(KnowledgeDocument, segment_count)
        .where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.scope_type == "person",
            KnowledgeDocument.scope_id == owner_user_id,
            KnowledgeDocument.status != "deleted",
            _personal_knowledge_access_predicate(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                current_user_id=current_user_id,
                agent_id=agent_id,
            ),
        )
        .order_by(KnowledgeDocument.updated_at.desc(), KnowledgeDocument.created_at.desc())
        .limit(max(1, int(limit or 50)))
    )
    if document_id is not None:
        statement = statement.where(KnowledgeDocument.id == document_id)
    return statement


def build_personal_knowledge_search_statement(
    *,
    tenant_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    query: str,
    current_user_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    limit: int,
):
    clean_query = _WHITESPACE_RE.sub(" ", str(query or "").strip())
    if not clean_query:
        raise ValueError("query must not be empty")

    ts_query = func.plainto_tsquery("simple", clean_query)
    score = func.coalesce(func.ts_rank_cd(KnowledgeSegment.tsv, ts_query), 0.0).label("score")
    like_query = f"%{_escape_like(clean_query)}%"
    search_predicate = or_(
        KnowledgeSegment.tsv.op("@@")(ts_query),
        KnowledgeSegment.content.ilike(like_query, escape="\\"),
        KnowledgeDocument.title.ilike(like_query, escape="\\"),
    )
    return (
        select(KnowledgeSegment, KnowledgeDocument, score)
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeSegment.document_id)
        .where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.scope_type == "person",
            KnowledgeDocument.scope_id == owner_user_id,
            KnowledgeDocument.status == "ready",
            KnowledgeDocument.agent_searchable.is_(True),
            KnowledgeSegment.tenant_id == tenant_id,
            KnowledgeSegment.scope_type == "person",
            KnowledgeSegment.scope_id == owner_user_id,
            search_predicate,
            _personal_knowledge_access_predicate(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                current_user_id=current_user_id,
                agent_id=agent_id,
            ),
        )
        .order_by(desc(score), KnowledgeDocument.updated_at.desc(), KnowledgeSegment.position.asc())
        .limit(max(1, int(limit or 5)))
    )


def _split_content(content: str, *, max_segment_chars: int, overlap_chars: int) -> list[str]:
    clean = content.strip()
    if not clean:
        return []
    if len(clean) <= max_segment_chars:
        return [clean]

    paragraphs = [part.strip() for part in re.split(r"\n{2,}", clean) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if not current:
            current = paragraph
            continue
        candidate = f"{current}\n\n{paragraph}"
        if len(candidate) <= max_segment_chars:
            current = candidate
            continue
        chunks.append(current)
        overlap = current[-overlap_chars:].strip() if overlap_chars > 0 else ""
        current = f"{overlap}\n\n{paragraph}".strip() if overlap else paragraph
    if current:
        chunks.append(current)

    split_chunks: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_segment_chars:
            split_chunks.append(chunk)
            continue
        start = 0
        while start < len(chunk):
            end = min(len(chunk), start + max_segment_chars)
            split_chunks.append(chunk[start:end].strip())
            if end == len(chunk):
                break
            start = max(0, end - overlap_chars)
    return [chunk for chunk in split_chunks if chunk]


def segment_markdown(markdown: str, *, max_segment_chars: int = 3600, overlap_chars: int = 400) -> list[KnowledgeSegmentDraft]:
    """Split Markdown into stable retrieval segments while preserving heading paths."""

    normalized = _normalize_markdown(markdown)
    headings: list[str] = []
    section_lines: list[str] = []
    section_heading_path: list[str] = []
    drafts: list[KnowledgeSegmentDraft] = []

    def flush_section() -> None:
        content = "\n".join(section_lines).strip()
        if not content:
            return
        for chunk in _split_content(content, max_segment_chars=max_segment_chars, overlap_chars=overlap_chars):
            position = len(drafts)
            heading_path = list(section_heading_path)
            segment_hash = _sha256("\n".join([*heading_path, chunk]))
            drafts.append(
                KnowledgeSegmentDraft(
                    position=position,
                    heading_path=heading_path,
                    content=chunk,
                    segment_hash=segment_hash,
                    token_count=_rough_token_count(chunk),
                )
            )

    for line in normalized.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            flush_section()
            section_lines = []
            level = len(match.group(1))
            title = _clean_title(match.group(2))
            headings = headings[: level - 1]
            headings.append(title)
            section_heading_path = list(headings)
            continue
        section_lines.append(line)

    flush_section()
    if drafts:
        return drafts

    fallback = normalized.strip()
    if not fallback:
        return []
    return [
        KnowledgeSegmentDraft(
            position=0,
            heading_path=[],
            content=fallback,
            segment_hash=_sha256(fallback),
            token_count=_rough_token_count(fallback),
        )
    ]


class PersonalKnowledgeService:
    """Write person-scope canonical Markdown into the Knowledge Core tables."""

    def __init__(self, *, data_root: str | Path | None = None, conversion_service: Any | None = None) -> None:
        self.data_root = Path(data_root) if data_root is not None else Path(get_settings().AGENT_DATA_DIR)
        self.conversion_service = conversion_service

    def _conversion_service(self) -> Any:
        if self.conversion_service is not None:
            return self.conversion_service
        from app.services.document_conversion import DocumentConversionService

        return DocumentConversionService()

    def _document_summary(
        self,
        *,
        owner_user_id: uuid.UUID,
        document: Any,
        segment_count: int,
    ) -> PersonalKnowledgeDocumentSummary:
        document_id = document.id
        return PersonalKnowledgeDocumentSummary(
            document_id=document_id,
            title=str(document.title or "Untitled knowledge document"),
            source_kind=str(document.source_kind or "unknown"),
            source_uri=document.source_uri,
            source_sha256=str(document.source_sha256 or ""),
            source_ref=f"kb://person/{owner_user_id}/documents/{document_id}",
            canonical_md_path=str(document.canonical_md_path or ""),
            status=str(document.status or "unknown"),
            sensitivity=str(document.sensitivity or "internal"),
            agent_searchable=bool(document.agent_searchable),
            segment_count=int(segment_count or 0),
            created_at=document.created_at,
            updated_at=document.updated_at,
            metadata=dict(document.doc_metadata_json or {}),
        )

    async def list_personal_documents(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        current_user_id: uuid.UUID | None,
        agent_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[PersonalKnowledgeDocumentSummary]:
        statement = build_personal_knowledge_document_list_statement(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            current_user_id=current_user_id,
            agent_id=agent_id,
            limit=limit,
        )
        rows = (await session.execute(statement)).all()
        return [
            self._document_summary(owner_user_id=owner_user_id, document=row[0], segment_count=row[1])
            for row in rows
        ]

    async def get_personal_document(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document_id: uuid.UUID,
        current_user_id: uuid.UUID | None,
        agent_id: uuid.UUID | None = None,
    ) -> PersonalKnowledgeDocumentDetail | None:
        statement = build_personal_knowledge_document_list_statement(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            current_user_id=current_user_id,
            agent_id=agent_id,
            limit=1,
            document_id=document_id,
        )
        rows = (await session.execute(statement)).all()
        if not rows:
            return None
        document, segment_count = rows[0][0], rows[0][1]
        segment_rows = (
            await session.execute(
                select(KnowledgeSegment)
                .where(
                    KnowledgeSegment.tenant_id == tenant_id,
                    KnowledgeSegment.document_id == document.id,
                    KnowledgeSegment.scope_type == "person",
                    KnowledgeSegment.scope_id == owner_user_id,
                )
                .order_by(KnowledgeSegment.position.asc())
            )
        ).all()
        segments: list[PersonalKnowledgeDocumentSegment] = []
        for row in segment_rows:
            try:
                segment = row[0]
            except (TypeError, KeyError):
                segment = row
            segments.append(
                PersonalKnowledgeDocumentSegment(
                    segment_id=segment.id,
                    position=int(segment.position),
                    heading_path=list(segment.heading_path_json or []),
                    content=str(segment.content or ""),
                    token_count=int(segment.token_count or 0),
                )
            )
        summary = self._document_summary(owner_user_id=owner_user_id, document=document, segment_count=segment_count)
        return PersonalKnowledgeDocumentDetail(**summary.__dict__, segments=segments)

    async def ingest_markdown(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        title: str,
        markdown: str,
        source_kind: str,
        source_uri: str | None = None,
        created_by_user_id: uuid.UUID | None = None,
        agent_searchable: bool = True,
        sensitivity: str = "internal",
        source_sha256: str | None = None,
        doc_metadata: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        force_reindex: bool = False,
    ) -> PersonalKnowledgeIngestResult:
        canonical_md = _normalize_markdown(markdown)
        if not canonical_md:
            raise ValueError("markdown must not be empty")

        clean_title = _clean_title(title)
        clean_source_kind = _clean_title(source_kind).lower().replace(" ", "_")
        source_payload = "\n".join([clean_source_kind, source_uri or "", canonical_md])
        clean_source_sha256 = str(source_sha256 or _sha256(source_payload)).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", clean_source_sha256):
            raise ValueError("source_sha256 must be a 64-character lowercase hex digest")
        artifact_hash = _sha256(canonical_md)
        artifact_path = personal_knowledge_artifact_path(self.data_root, owner_user_id, clean_source_sha256)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(canonical_md, encoding="utf-8")
        canonical_md_path = artifact_path.relative_to(self.data_root).as_posix()

        existing_result = await session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.scope_type == "person",
                KnowledgeDocument.scope_id == owner_user_id,
                KnowledgeDocument.source_sha256 == clean_source_sha256,
            )
        )
        document = existing_result.scalar_one_or_none()
        previous_artifact_hash = document.canonical_md_sha256 if document is not None else None
        if document is None:
            document = KnowledgeDocument(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                scope_type="person",
                scope_id=owner_user_id,
                owner_user_id=owner_user_id,
                source_kind=clean_source_kind,
                source_uri=source_uri,
                source_sha256=clean_source_sha256,
                artifact_hash=artifact_hash,
                title=clean_title,
                status="ready",
                sensitivity=sensitivity,
                agent_searchable=agent_searchable,
                canonical_md_path=canonical_md_path,
                canonical_md_sha256=artifact_hash,
                doc_metadata_json={"ingest_format": "canonical_markdown", **(doc_metadata or {})},
                created_by_user_id=created_by_user_id,
            )
            session.add(document)
        else:
            document.source_kind = clean_source_kind
            document.source_uri = source_uri
            document.artifact_hash = artifact_hash
            document.title = clean_title
            document.status = "ready"
            document.sensitivity = sensitivity
            document.agent_searchable = agent_searchable
            document.canonical_md_path = canonical_md_path
            document.canonical_md_sha256 = artifact_hash
            document.doc_metadata_json = {
                **(document.doc_metadata_json or {}),
                "ingest_format": "canonical_markdown",
                **(doc_metadata or {}),
            }
            document.created_by_user_id = created_by_user_id or document.created_by_user_id
        await session.flush()

        await session.execute(delete(KnowledgeSegment).where(KnowledgeSegment.document_id == document.id))

        segment_drafts = segment_markdown(canonical_md)
        for draft in segment_drafts:
            session.add(
                KnowledgeSegment(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    document_id=document.id,
                    scope_type="person",
                    scope_id=owner_user_id,
                    position=draft.position,
                    segment_hash=draft.segment_hash,
                    heading_path_json=draft.heading_path,
                    content=draft.content,
                    token_count=draft.token_count,
                    segment_metadata_json={},
                )
            )

        job_id: uuid.UUID | None = None
        if previous_artifact_hash != artifact_hash or force_reindex:
            job = KnowledgeIndexJob(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                document_id=document.id,
                scope_type="person",
                scope_id=owner_user_id,
                artifact_hash=artifact_hash,
                stage="indexed",
                status="ready",
                attempt_count=1,
                job_metadata_json={
                    "channels": ["tsvector", "segments"],
                    "source_kind": clean_source_kind,
                    "warnings": list(warnings or []),
                },
            )
            session.add(job)
            job_id = job.id
        await session.flush()

        await session.execute(
            update(KnowledgeSegment)
            .where(KnowledgeSegment.document_id == document.id)
            .values(tsv=func.to_tsvector("simple", KnowledgeSegment.content))
        )

        return PersonalKnowledgeIngestResult(
            document_id=document.id,
            job_id=job_id,
            source_sha256=clean_source_sha256,
            artifact_hash=artifact_hash,
            canonical_md_path=canonical_md_path,
            segment_count=len(segment_drafts),
            status="ready",
            warnings=list(warnings or []),
        )

    async def ingest_source_bytes(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        filename: str,
        data: bytes,
        title: str | None = None,
        source_kind: str = "upload",
        source_uri: str | None = None,
        created_by_user_id: uuid.UUID | None = None,
        agent_searchable: bool = True,
        sensitivity: str = "internal",
        source_mime_type: str | None = None,
    ) -> PersonalKnowledgeIngestResult:
        safe_name = _safe_filename(filename)
        ext = _extension_for_filename(safe_name)
        source_hash = _sha256_bytes(data)
        artifact_hash = source_hash
        if ext not in _SUPPORTED_IMPORT_EXTENSIONS:
            document = KnowledgeDocument(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                scope_type="person",
                scope_id=owner_user_id,
                owner_user_id=owner_user_id,
                source_kind=source_kind,
                source_uri=source_uri,
                source_sha256=source_hash,
                artifact_hash=artifact_hash,
                title=_clean_title(safe_name),
                status="failed",
                sensitivity=sensitivity,
                agent_searchable=agent_searchable,
                canonical_md_path="",
                canonical_md_sha256=None,
                doc_metadata_json={"source_filename": safe_name, "error": "unsupported_file_type"},
                created_by_user_id=created_by_user_id,
            )
            session.add(document)
            await session.flush()
            job = KnowledgeIndexJob(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                document_id=document.id,
                scope_type="person",
                scope_id=owner_user_id,
                artifact_hash=artifact_hash,
                stage="converting",
                status="failed",
                error_message=f"unsupported_file_type:{ext or 'unknown'}",
                attempt_count=1,
                job_metadata_json={"source_kind": source_kind, "source_filename": safe_name},
            )
            session.add(job)
            await session.flush()
            return PersonalKnowledgeIngestResult(
                document_id=document.id,
                job_id=job.id,
                source_sha256=source_hash,
                artifact_hash=artifact_hash,
                canonical_md_path="",
                segment_count=0,
                status="failed",
                warnings=[job.error_message or "unsupported_file_type"],
            )

        workspace_root = _personal_knowledge_root(self.data_root, owner_user_id)
        conversion = self._conversion_service().convert_bytes(
            data=data,
            filename=safe_name,
            workspace_root=workspace_root,
            source_uri=source_uri,
            source_mime_type=source_mime_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream",
            tenant_id=tenant_id,
            agent_id=None,
            user_id=owner_user_id,
            mode="auto",
            force_refresh=False,
        )
        warnings = list(getattr(conversion, "warnings", ()) or [])
        metadata = {
            "source_filename": safe_name,
            "source_kind": source_kind,
            "conversion_engine": getattr(conversion, "engine", "unknown"),
            "conversion_warnings": warnings,
            "conversion_markdown_path": getattr(conversion, "artifact_markdown_path", ""),
            "conversion_metadata_path": getattr(conversion, "artifact_metadata_path", ""),
            "source_mime_type": getattr(conversion, "source_mime_type", source_mime_type or ""),
        }
        return await self.ingest_markdown(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            title=title_from_filename_or_uri(safe_name, source_uri, title),
            markdown=str(getattr(conversion, "markdown", "") or ""),
            source_kind=source_kind,
            source_uri=source_uri,
            created_by_user_id=created_by_user_id,
            agent_searchable=agent_searchable,
            sensitivity=sensitivity,
            source_sha256=getattr(conversion, "source_sha256", source_hash),
            doc_metadata=metadata,
            warnings=warnings,
        )

    async def ingest_url(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        url: str,
        title: str | None = None,
        created_by_user_id: uuid.UUID | None = None,
        agent_searchable: bool = True,
        sensitivity: str = "internal",
    ) -> PersonalKnowledgeIngestResult:
        clean_url = str(url or "").strip()
        parsed = urlparse(clean_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an absolute http(s) URL")

        import httpx

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(clean_url)
            response.raise_for_status()
        filename = _safe_filename(Path(parsed.path).name or "imported-url.html")
        return await self.ingest_source_bytes(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            filename=filename,
            data=response.content,
            title=title,
            source_kind="url",
            source_uri=clean_url,
            created_by_user_id=created_by_user_id,
            agent_searchable=agent_searchable,
            sensitivity=sensitivity,
            source_mime_type=response.headers.get("content-type"),
        )

    async def list_import_jobs(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        limit: int = 50,
    ) -> list[PersonalKnowledgeJobSummary]:
        rows = (
            await session.execute(
                select(KnowledgeIndexJob)
                .where(
                    KnowledgeIndexJob.tenant_id == tenant_id,
                    KnowledgeIndexJob.scope_type == "person",
                    KnowledgeIndexJob.scope_id == owner_user_id,
                )
                .order_by(KnowledgeIndexJob.updated_at.desc(), KnowledgeIndexJob.created_at.desc())
                .limit(max(1, int(limit or 50)))
            )
        ).all()
        jobs: list[PersonalKnowledgeJobSummary] = []
        for row in rows:
            job = row[0] if isinstance(row, tuple) else row
            jobs.append(
                PersonalKnowledgeJobSummary(
                    job_id=job.id,
                    document_id=job.document_id,
                    stage=str(job.stage or ""),
                    status=str(job.status or ""),
                    artifact_hash=str(job.artifact_hash or ""),
                    error_message=job.error_message,
                    attempt_count=int(job.attempt_count or 0),
                    metadata=dict(job.job_metadata_json or {}),
                    created_at=job.created_at,
                    updated_at=job.updated_at,
                )
            )
        return jobs

    async def patch_personal_document(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document_id: uuid.UUID,
        current_user_id: uuid.UUID | None,
        agent_id: uuid.UUID | None,
        agent_searchable: bool | None = None,
        sensitivity: str | None = None,
        status: str | None = None,
    ) -> PersonalKnowledgeDocumentSummary | None:
        if current_user_id != owner_user_id:
            return None
        result = await session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.scope_type == "person",
                KnowledgeDocument.scope_id == owner_user_id,
                KnowledgeDocument.id == document_id,
            )
        )
        document = result.scalar_one_or_none()
        if document is None:
            return None
        if agent_searchable is not None:
            document.agent_searchable = bool(agent_searchable)
        if sensitivity is not None:
            document.sensitivity = str(sensitivity)
        if status is not None:
            document.status = str(status)
        await session.flush()
        return self._document_summary(owner_user_id=owner_user_id, document=document, segment_count=0)

    async def rebuild_personal_document_index(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document_id: uuid.UUID,
        current_user_id: uuid.UUID | None,
    ) -> PersonalKnowledgeIngestResult | None:
        if current_user_id != owner_user_id:
            return None
        result = await session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.scope_type == "person",
                KnowledgeDocument.scope_id == owner_user_id,
                KnowledgeDocument.id == document_id,
            )
        )
        document = result.scalar_one_or_none()
        if document is None:
            return None
        artifact_path = self.data_root / str(document.canonical_md_path or "")
        if not artifact_path.exists():
            job = KnowledgeIndexJob(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                document_id=document.id,
                scope_type="person",
                scope_id=owner_user_id,
                artifact_hash=str(document.artifact_hash or document.source_sha256),
                stage="indexing",
                status="failed",
                error_message="canonical_markdown_missing",
                attempt_count=1,
                job_metadata_json={"source_kind": document.source_kind},
            )
            session.add(job)
            await session.flush()
            return PersonalKnowledgeIngestResult(
                document_id=document.id,
                job_id=job.id,
                source_sha256=str(document.source_sha256),
                artifact_hash=str(document.artifact_hash or ""),
                canonical_md_path=str(document.canonical_md_path or ""),
                segment_count=0,
                status="failed",
                warnings=["canonical_markdown_missing"],
            )
        return await self.ingest_markdown(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            title=str(document.title or "Untitled knowledge document"),
            markdown=artifact_path.read_text(encoding="utf-8"),
            source_kind=str(document.source_kind or "rebuild"),
            source_uri=document.source_uri,
            created_by_user_id=document.created_by_user_id,
            agent_searchable=bool(document.agent_searchable),
            sensitivity=str(document.sensitivity or "internal"),
            source_sha256=str(document.source_sha256),
            doc_metadata=dict(document.doc_metadata_json or {}),
            force_reindex=True,
        )

    async def retry_import_job(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        job_id: uuid.UUID,
        current_user_id: uuid.UUID | None,
    ) -> PersonalKnowledgeIngestResult | None:
        if current_user_id != owner_user_id:
            return None
        result = await session.execute(
            select(KnowledgeIndexJob).where(
                KnowledgeIndexJob.tenant_id == tenant_id,
                KnowledgeIndexJob.scope_type == "person",
                KnowledgeIndexJob.scope_id == owner_user_id,
                KnowledgeIndexJob.id == job_id,
            )
        )
        job = result.scalar_one_or_none()
        if job is None:
            return None
        return await self.rebuild_personal_document_index(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            document_id=job.document_id,
            current_user_id=current_user_id,
        )

    async def search_personal(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        query: str,
        current_user_id: uuid.UUID | None,
        agent_id: uuid.UUID | None = None,
        limit: int = 5,
    ) -> list[KnowledgeSearchHit]:
        statement = build_personal_knowledge_search_statement(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            query=query,
            current_user_id=current_user_id,
            agent_id=agent_id,
            limit=limit,
        )
        rows = (await session.execute(statement)).all()
        hits: list[KnowledgeSearchHit] = []
        for row in rows:
            segment, document, score = row[0], row[1], row[2]
            source_ref = f"kb://person/{owner_user_id}/documents/{document.id}#segment={segment.id}"
            content = str(segment.content or "").strip()
            snippet = content[:500]
            hits.append(
                KnowledgeSearchHit(
                    document_id=document.id,
                    segment_id=segment.id,
                    title=str(document.title or "Untitled knowledge document"),
                    snippet=snippet,
                    source_ref=source_ref,
                    score=float(score or 0.0),
                    heading_path=list(segment.heading_path_json or []),
                    sensitivity=str(document.sensitivity or "internal"),
                    metadata={
                        "canonical_md_path": document.canonical_md_path,
                        "source_sha256": document.source_sha256,
                    },
                )
            )
        return hits
