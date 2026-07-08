"""Personal Knowledge Base ingestion and search primitives."""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import Text, and_, cast, delete, desc, exists, false, func, or_, select, true, update

from app.config import get_settings
from app.models.knowledge import (
    KnowledgeAssertion,
    KnowledgeDocument,
    KnowledgeEntity,
    KnowledgeGrant,
    KnowledgeIndexJob,
    KnowledgeLink,
    KnowledgeSegment,
)


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
_DEFAULT_EXTRACTOR = object()
_SENSITIVE_EXTRACTION_BLOCKLIST = {"private", "secret", "restricted", "pl3", "pl4", "credential"}


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
    score_trace: dict[str, Any] = field(default_factory=dict)


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


def _clean_graph_text(value: Any, *, max_len: int = 300) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "").strip())[:max_len]


def _coerce_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(1.0, number))


def _merge_unique_strings(existing: list[Any] | tuple[Any, ...] | None, additions: list[Any] | tuple[Any, ...]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in [*(existing or []), *additions]:
        clean = _clean_graph_text(value)
        key = clean.lower()
        if not clean or key in seen:
            continue
        seen.add(key)
        merged.append(clean)
    return merged


def _merge_source_refs(existing: list[Any] | tuple[Any, ...] | None, additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in [*(existing or []), *additions]:
        if not isinstance(ref, dict):
            continue
        clean_ref = {str(key): value for key, value in ref.items() if value is not None}
        key = json.dumps(clean_ref, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        merged.append(clean_ref)
    return merged


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
            KnowledgeDocument.status.in_(("ready", "degraded")),
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


def _row_first(row: Any) -> Any:
    if isinstance(row, tuple):
        return row[0]
    try:
        return row[0]
    except (TypeError, KeyError, IndexError):
        return row


def _source_ref_segment_ids(refs: Any) -> list[uuid.UUID]:
    segment_ids: list[uuid.UUID] = []
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        raw_id = ref.get("segment_id")
        try:
            segment_ids.append(uuid.UUID(str(raw_id)))
        except (TypeError, ValueError):
            continue
    return segment_ids


def _freshness_boost(updated_at: Any) -> float:
    if not isinstance(updated_at, datetime):
        return 0.0
    timestamp = updated_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds() / 86400.0)
    return min(0.03, 0.03 / (1.0 + age_days / 30.0))


def _heat_boost(metadata: dict[str, Any]) -> float:
    raw = metadata.get("citation_count", metadata.get("usage_count", metadata.get("reference_count", 0)))
    try:
        count = max(0.0, float(raw or 0.0))
    except (TypeError, ValueError):
        return 0.0
    return min(0.05, math.log1p(count) * 0.01)


class PersonalKnowledgeService:
    """Write person-scope canonical Markdown into the Knowledge Core tables."""

    def __init__(
        self,
        *,
        data_root: str | Path | None = None,
        conversion_service: Any | None = None,
        extractor: Any = _DEFAULT_EXTRACTOR,
    ) -> None:
        self.data_root = Path(data_root) if data_root is not None else Path(get_settings().AGENT_DATA_DIR)
        self.conversion_service = conversion_service
        self.extractor = extractor

    def _conversion_service(self) -> Any:
        if self.conversion_service is not None:
            return self.conversion_service
        from app.services.document_conversion import DocumentConversionService

        return DocumentConversionService()

    def _knowledge_extractor(self) -> Any | None:
        if self.extractor is None:
            return None
        if self.extractor is not _DEFAULT_EXTRACTOR:
            return self.extractor
        from app.services.personal_knowledge_extractor import PersonalKnowledgeLLMExtractor

        return PersonalKnowledgeLLMExtractor()

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

    async def _upsert_index_job(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document_id: uuid.UUID,
        artifact_hash: str,
        source_kind: str,
        warnings: list[str],
        stage: str,
        status: str,
        error_message: str | None = None,
        channels: list[str] | None = None,
        attempt_increment: int = 1,
    ) -> KnowledgeIndexJob:
        result = await session.execute(
            select(KnowledgeIndexJob).where(
                KnowledgeIndexJob.tenant_id == tenant_id,
                KnowledgeIndexJob.document_id == document_id,
                KnowledgeIndexJob.artifact_hash == artifact_hash,
            )
        )
        job = result.scalar_one_or_none()
        if not isinstance(job, KnowledgeIndexJob):
            job = KnowledgeIndexJob(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                document_id=document_id,
                scope_type="person",
                scope_id=owner_user_id,
                artifact_hash=artifact_hash,
                attempt_count=max(1, int(attempt_increment or 1)),
            )
            session.add(job)
        else:
            job.attempt_count = int(job.attempt_count or 0) + max(1, int(attempt_increment or 1))
        job.stage = stage
        job.status = status
        job.error_message = error_message
        job.job_metadata_json = {
            **(job.job_metadata_json or {}),
            "channels": channels or ["tsvector", "segments"],
            "source_kind": source_kind,
            "warnings": list(warnings),
        }
        await session.flush()
        return job

    async def _clear_document_graph_projection(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> None:
        document_ref = [{"document_id": str(document_id)}]
        await session.execute(
            delete(KnowledgeAssertion).where(
                KnowledgeAssertion.tenant_id == tenant_id,
                KnowledgeAssertion.scope_type == "person",
                KnowledgeAssertion.scope_id == owner_user_id,
                KnowledgeAssertion.source_document_id == document_id,
            )
        )
        await session.execute(
            delete(KnowledgeLink).where(
                KnowledgeLink.tenant_id == tenant_id,
                KnowledgeLink.scope_type == "person",
                KnowledgeLink.scope_id == owner_user_id,
                KnowledgeLink.source_refs_json.contains(document_ref),
            )
        )

    async def _upsert_entity(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        canonical_name: str,
        entity_type: str,
        aliases: list[str],
        description: str | None,
        confidence: float,
        source_ref: dict[str, Any],
    ) -> KnowledgeEntity | None:
        clean_name = _clean_graph_text(canonical_name)
        clean_type = _clean_graph_text(entity_type, max_len=80) or "freeform"
        if not clean_name:
            return None
        result = await session.execute(
            select(KnowledgeEntity).where(
                KnowledgeEntity.tenant_id == tenant_id,
                KnowledgeEntity.scope_type == "person",
                KnowledgeEntity.scope_id == owner_user_id,
                KnowledgeEntity.entity_type == clean_type,
                KnowledgeEntity.canonical_name == clean_name,
            )
        )
        entity = result.scalar_one_or_none()
        if not isinstance(entity, KnowledgeEntity):
            entity = KnowledgeEntity(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                scope_type="person",
                scope_id=owner_user_id,
                canonical_name=clean_name,
                entity_type=clean_type,
                aliases_json=[],
                source_refs_json=[],
            )
            session.add(entity)
        entity.aliases_json = _merge_unique_strings(entity.aliases_json, aliases)
        entity.description = description or entity.description
        entity.confidence = max(_coerce_confidence(entity.confidence), _coerce_confidence(confidence))
        entity.source_refs_json = _merge_source_refs(entity.source_refs_json, [source_ref])
        await session.flush()

        for alias in entity.aliases_json:
            alias_result = await session.execute(
                select(KnowledgeEntity).where(
                    KnowledgeEntity.tenant_id == tenant_id,
                    KnowledgeEntity.scope_type == "person",
                    KnowledgeEntity.scope_id == owner_user_id,
                    KnowledgeEntity.entity_type == clean_type,
                    KnowledgeEntity.canonical_name == alias,
                    KnowledgeEntity.id != entity.id,
                )
            )
            alias_entity = alias_result.scalar_one_or_none()
            if isinstance(alias_entity, KnowledgeEntity):
                alias_entity.merged_into_entity_id = entity.id
                alias_entity.source_refs_json = _merge_source_refs(alias_entity.source_refs_json, [source_ref])
        return entity

    async def _upsert_assertion(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document_id: uuid.UUID,
        subject_text: str,
        predicate: str,
        object_text: str,
        confidence: float,
        source_ref: dict[str, Any],
        entity_by_name: dict[str, KnowledgeEntity],
    ) -> KnowledgeAssertion | None:
        subject = _clean_graph_text(subject_text)
        pred = _clean_graph_text(predicate, max_len=120)
        obj = _clean_graph_text(object_text, max_len=2000)
        if not subject or not pred or not obj:
            return None
        result = await session.execute(
            select(KnowledgeAssertion).where(
                KnowledgeAssertion.tenant_id == tenant_id,
                KnowledgeAssertion.scope_type == "person",
                KnowledgeAssertion.scope_id == owner_user_id,
                KnowledgeAssertion.subject_text == subject,
                KnowledgeAssertion.predicate == pred,
                KnowledgeAssertion.object_text == obj,
            )
        )
        assertion = result.scalar_one_or_none()
        subject_entity = entity_by_name.get(subject.lower())
        object_entity = entity_by_name.get(obj.lower())
        if not isinstance(assertion, KnowledgeAssertion):
            assertion = KnowledgeAssertion(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                scope_type="person",
                scope_id=owner_user_id,
                source_document_id=document_id,
                subject_text=subject,
                predicate=pred,
                object_text=obj,
                source_refs_json=[],
            )
            session.add(assertion)
        assertion.source_document_id = document_id
        assertion.subject_entity_id = subject_entity.id if subject_entity else assertion.subject_entity_id
        assertion.object_entity_id = object_entity.id if object_entity else assertion.object_entity_id
        assertion.confidence = max(_coerce_confidence(assertion.confidence), _coerce_confidence(confidence))
        assertion.status = "active"
        assertion.source_refs_json = _merge_source_refs(assertion.source_refs_json, [source_ref])
        return assertion

    async def _upsert_link(
        self,
        session: Any,
        *,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        relation: str,
        confidence: float,
        source_ref: dict[str, Any],
        from_entity: KnowledgeEntity,
        to_entity: KnowledgeEntity,
    ) -> KnowledgeLink | None:
        clean_relation = _clean_graph_text(relation, max_len=80)
        if not clean_relation:
            return None
        result = await session.execute(
            select(KnowledgeLink).where(
                KnowledgeLink.tenant_id == tenant_id,
                KnowledgeLink.scope_type == "person",
                KnowledgeLink.scope_id == owner_user_id,
                KnowledgeLink.from_kind == "entity",
                KnowledgeLink.from_id == from_entity.id,
                KnowledgeLink.to_kind == "entity",
                KnowledgeLink.to_id == to_entity.id,
                KnowledgeLink.relation == clean_relation,
            )
        )
        link = result.scalar_one_or_none()
        if not isinstance(link, KnowledgeLink):
            link = KnowledgeLink(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                scope_type="person",
                scope_id=owner_user_id,
                from_kind="entity",
                from_id=from_entity.id,
                to_kind="entity",
                to_id=to_entity.id,
                relation=clean_relation,
                source_refs_json=[],
            )
            session.add(link)
        link.confidence = max(_coerce_confidence(link.confidence), _coerce_confidence(confidence))
        link.source_refs_json = _merge_source_refs(link.source_refs_json, [source_ref])
        return link

    async def _extract_and_write_graph(
        self,
        session: Any,
        *,
        extractor: Any,
        tenant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        document: KnowledgeDocument,
        segments: list[KnowledgeSegment],
        sensitivity: str,
    ) -> list[str]:
        warnings: list[str] = []
        entity_by_name: dict[str, KnowledgeEntity] = {}
        for segment in segments:
            source_ref = {
                "document_id": str(document.id),
                "segment_id": str(segment.id),
                "seg_hash": str(segment.segment_hash),
                "heading_path": list(segment.heading_path_json or []),
                "position": int(segment.position),
            }
            extraction = await extractor.extract_segment(
                segment=segment,
                document=document,
                source_ref=source_ref,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                sensitivity=sensitivity,
            )
            warnings.extend([str(warning) for warning in getattr(extraction, "warnings", ()) or []])

            for extracted_entity in getattr(extraction, "entities", ()) or []:
                entity = await self._upsert_entity(
                    session,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    canonical_name=getattr(extracted_entity, "canonical_name", ""),
                    entity_type=getattr(extracted_entity, "entity_type", "freeform"),
                    aliases=list(getattr(extracted_entity, "aliases", ()) or []),
                    description=getattr(extracted_entity, "description", None),
                    confidence=getattr(extracted_entity, "confidence", 1.0),
                    source_ref=source_ref,
                )
                if entity is None:
                    continue
                entity_by_name[entity.canonical_name.lower()] = entity
                for alias in entity.aliases_json:
                    entity_by_name[alias.lower()] = entity

            for extracted_assertion in getattr(extraction, "assertions", ()) or []:
                await self._upsert_assertion(
                    session,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    document_id=document.id,
                    subject_text=getattr(extracted_assertion, "subject_text", ""),
                    predicate=getattr(extracted_assertion, "predicate", ""),
                    object_text=getattr(extracted_assertion, "object_text", ""),
                    confidence=getattr(extracted_assertion, "confidence", 1.0),
                    source_ref=source_ref,
                    entity_by_name=entity_by_name,
                )

            for extracted_link in getattr(extraction, "links", ()) or []:
                from_entity = entity_by_name.get(_clean_graph_text(getattr(extracted_link, "from_name", "")).lower())
                if from_entity is None:
                    from_entity = await self._upsert_entity(
                        session,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        canonical_name=getattr(extracted_link, "from_name", ""),
                        entity_type=getattr(extracted_link, "from_type", "freeform"),
                        aliases=[],
                        description=None,
                        confidence=getattr(extracted_link, "confidence", 1.0),
                        source_ref=source_ref,
                    )
                to_entity = entity_by_name.get(_clean_graph_text(getattr(extracted_link, "to_name", "")).lower())
                if to_entity is None:
                    to_entity = await self._upsert_entity(
                        session,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        canonical_name=getattr(extracted_link, "to_name", ""),
                        entity_type=getattr(extracted_link, "to_type", "freeform"),
                        aliases=[],
                        description=None,
                        confidence=getattr(extracted_link, "confidence", 1.0),
                        source_ref=source_ref,
                    )
                if from_entity is None or to_entity is None:
                    continue
                entity_by_name[from_entity.canonical_name.lower()] = from_entity
                entity_by_name[to_entity.canonical_name.lower()] = to_entity
                await self._upsert_link(
                    session,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    relation=getattr(extracted_link, "relation", ""),
                    confidence=getattr(extracted_link, "confidence", 1.0),
                    source_ref=source_ref,
                    from_entity=from_entity,
                    to_entity=to_entity,
                )
        await session.flush()
        return warnings

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
        segment_objects: list[KnowledgeSegment] = []
        for draft in segment_drafts:
            segment = KnowledgeSegment(
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
            session.add(segment)
            segment_objects.append(segment)
        await session.flush()

        job_id: uuid.UUID | None = None
        job: KnowledgeIndexJob | None = None
        all_warnings = list(warnings or [])
        final_status = "ready"
        final_stage = "indexed"
        final_error: str | None = None
        channels = ["tsvector", "segments"]
        if previous_artifact_hash != artifact_hash or force_reindex:
            job = await self._upsert_index_job(
                session,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                document_id=document.id,
                artifact_hash=artifact_hash,
                source_kind=clean_source_kind,
                warnings=all_warnings,
                stage="segmenting",
                status="running",
                channels=channels,
            )
            job_id = job.id

        extractor = self._knowledge_extractor()
        if extractor is not None:
            if str(sensitivity or "").lower() in _SENSITIVE_EXTRACTION_BLOCKLIST:
                final_status = "degraded"
                final_stage = "extracting"
                final_error = "knowledge_extraction_skipped_sensitive"
                all_warnings.append(final_error)
            else:
                try:
                    await self._clear_document_graph_projection(
                        session,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        document_id=document.id,
                    )
                    extraction_warnings = await self._extract_and_write_graph(
                        session,
                        extractor=extractor,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        document=document,
                        segments=segment_objects,
                        sensitivity=sensitivity,
                    )
                    all_warnings.extend(extraction_warnings)
                    channels = ["tsvector", "segments", "graph"]
                except Exception as exc:
                    final_status = "degraded"
                    final_stage = "extracting"
                    final_error = f"knowledge_extraction_failed:{exc}"
                    all_warnings.append(final_error)

        document.status = final_status
        document.doc_metadata_json = {
            **(document.doc_metadata_json or {}),
            "ingest_format": "canonical_markdown",
            "warnings": all_warnings,
            **(doc_metadata or {}),
        }
        if job is not None:
            job.stage = final_stage
            job.status = final_status
            job.error_message = final_error
            job.job_metadata_json = {
                **(job.job_metadata_json or {}),
                "channels": channels,
                "source_kind": clean_source_kind,
                "warnings": all_warnings,
            }

        await session.execute(
            update(KnowledgeSegment)
            .where(KnowledgeSegment.document_id == document.id)
            .values(tsv=func.to_tsvector("simple", KnowledgeSegment.content))
        )
        await session.flush()

        return PersonalKnowledgeIngestResult(
            document_id=document.id,
            job_id=job_id,
            source_sha256=clean_source_sha256,
            artifact_hash=artifact_hash,
            canonical_md_path=canonical_md_path,
            segment_count=len(segment_drafts),
            status=final_status,
            warnings=all_warnings,
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
        clean_query = _WHITESPACE_RE.sub(" ", str(query or "").strip())
        if not clean_query:
            raise ValueError("query must not be empty")
        result_limit = max(1, int(limit or 5))
        candidate_limit = max(result_limit * 3, 10)

        candidates: dict[uuid.UUID, dict[str, Any]] = {}

        def add_candidate(
            *,
            segment: Any,
            document: Any,
            channel: str,
            rank: int,
            raw_score: float,
        ) -> None:
            segment_id = segment.id
            document_metadata = dict(getattr(document, "doc_metadata_json", None) or {})
            source_ref = f"kb://person/{owner_user_id}/documents/{document.id}#segment={segment.id}"
            entry = candidates.setdefault(
                segment_id,
                {
                    "segment": segment,
                    "document": document,
                    "source_ref": source_ref,
                    "channels": {},
                    "rrf": 0.0,
                    "boosts": {
                        "heat": _heat_boost(document_metadata),
                        "freshness": _freshness_boost(getattr(document, "updated_at", None)),
                    },
                },
            )
            channel_trace = entry["channels"].setdefault(channel, {"rank": rank, "raw_score": raw_score})
            channel_trace["rank"] = min(int(channel_trace["rank"]), int(rank))
            channel_trace["raw_score"] = max(float(channel_trace["raw_score"]), float(raw_score or 0.0))
            entry["rrf"] += 1.0 / (60.0 + max(1, int(rank)))

        text_statement = build_personal_knowledge_search_statement(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            query=clean_query,
            current_user_id=current_user_id,
            agent_id=agent_id,
            limit=candidate_limit,
        )
        text_rows = (await session.execute(text_statement)).all()
        for rank, row in enumerate(text_rows, start=1):
            segment, document, score = row[0], row[1], row[2]
            add_candidate(segment=segment, document=document, channel="text", rank=rank, raw_score=float(score or 0.0))

        like_query = f"%{_escape_like(clean_query)}%"
        entity_rows = (
            await session.execute(
                select(KnowledgeEntity)
                .where(
                    KnowledgeEntity.tenant_id == tenant_id,
                    KnowledgeEntity.scope_type == "person",
                    KnowledgeEntity.scope_id == owner_user_id,
                    KnowledgeEntity.merged_into_entity_id.is_(None),
                    or_(
                        KnowledgeEntity.canonical_name.ilike(like_query, escape="\\"),
                        cast(KnowledgeEntity.aliases_json, Text).ilike(like_query, escape="\\"),
                    ),
                )
                .order_by(KnowledgeEntity.confidence.desc(), KnowledgeEntity.updated_at.desc())
                .limit(candidate_limit)
            )
        ).all()
        entities = [entity for row in entity_rows if isinstance((entity := _row_first(row)), KnowledgeEntity) or hasattr(entity, "source_refs_json")]
        entity_segment_ids: list[uuid.UUID] = []
        for entity in entities:
            entity_segment_ids.extend(_source_ref_segment_ids(getattr(entity, "source_refs_json", None)))

        graph_segment_ids: list[uuid.UUID] = []
        if entities:
            entity_ids = [entity.id for entity in entities if getattr(entity, "id", None) is not None]
            if entity_ids:
                graph_rows = (
                    await session.execute(
                        select(KnowledgeLink)
                        .where(
                            KnowledgeLink.tenant_id == tenant_id,
                            KnowledgeLink.scope_type == "person",
                            KnowledgeLink.scope_id == owner_user_id,
                            or_(KnowledgeLink.from_id.in_(entity_ids), KnowledgeLink.to_id.in_(entity_ids)),
                        )
                        .order_by(KnowledgeLink.confidence.desc())
                        .limit(candidate_limit)
                    )
                ).all()
                for row in graph_rows:
                    link = _row_first(row)
                    if not hasattr(link, "source_refs_json"):
                        continue
                    graph_segment_ids.extend(_source_ref_segment_ids(getattr(link, "source_refs_json", None)))

        fetch_segment_ids = list(dict.fromkeys([*entity_segment_ids, *graph_segment_ids]))
        if fetch_segment_ids:
            segment_rows = (
                await session.execute(
                    select(KnowledgeSegment, KnowledgeDocument)
                    .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeSegment.document_id)
                    .where(
                        KnowledgeDocument.tenant_id == tenant_id,
                        KnowledgeDocument.scope_type == "person",
                        KnowledgeDocument.scope_id == owner_user_id,
                        KnowledgeDocument.status.in_(("ready", "degraded")),
                        KnowledgeDocument.agent_searchable.is_(True),
                        KnowledgeSegment.tenant_id == tenant_id,
                        KnowledgeSegment.scope_type == "person",
                        KnowledgeSegment.scope_id == owner_user_id,
                        KnowledgeSegment.id.in_(fetch_segment_ids),
                        _personal_knowledge_access_predicate(
                            tenant_id=tenant_id,
                            owner_user_id=owner_user_id,
                            current_user_id=current_user_id,
                            agent_id=agent_id,
                        ),
                    )
                )
            ).all()
            entity_rank_by_segment = {segment_id: rank for rank, segment_id in enumerate(entity_segment_ids, start=1)}
            graph_rank_by_segment = {segment_id: rank for rank, segment_id in enumerate(graph_segment_ids, start=1)}
            for row in segment_rows:
                segment, document = row[0], row[1]
                if segment.id in entity_rank_by_segment:
                    add_candidate(
                        segment=segment,
                        document=document,
                        channel="entity",
                        rank=entity_rank_by_segment[segment.id],
                        raw_score=1.0,
                    )
                if segment.id in graph_rank_by_segment:
                    add_candidate(
                        segment=segment,
                        document=document,
                        channel="graph",
                        rank=graph_rank_by_segment[segment.id],
                        raw_score=1.0,
                    )

        hits: list[KnowledgeSearchHit] = []
        ranked_entries = sorted(
            candidates.values(),
            key=lambda entry: (
                float(entry["rrf"]) + float(entry["boosts"]["heat"]) + float(entry["boosts"]["freshness"]),
                max((trace["raw_score"] for trace in entry["channels"].values()), default=0.0),
            ),
            reverse=True,
        )[:result_limit]
        for entry in ranked_entries:
            segment, document = entry["segment"], entry["document"]
            boosts = dict(entry["boosts"])
            final_score = float(entry["rrf"]) + float(boosts["heat"]) + float(boosts["freshness"])
            content = str(segment.content or "").strip()
            snippet = content[:500]
            document_metadata = dict(getattr(document, "doc_metadata_json", None) or {})
            hits.append(
                KnowledgeSearchHit(
                    document_id=document.id,
                    segment_id=segment.id,
                    title=str(document.title or "Untitled knowledge document"),
                    snippet=snippet,
                    source_ref=entry["source_ref"],
                    score=final_score,
                    heading_path=list(segment.heading_path_json or []),
                    sensitivity=str(document.sensitivity or "internal"),
                    metadata={
                        "canonical_md_path": document.canonical_md_path,
                        "source_sha256": document.source_sha256,
                    },
                    score_trace={
                        "channels": dict(entry["channels"]),
                        "rrf": float(entry["rrf"]),
                        "boosts": boosts,
                        "final": final_score,
                        "document_status": str(getattr(document, "status", "ready") or "ready"),
                        "document_metadata": {
                            key: document_metadata[key]
                            for key in ("citation_count", "usage_count", "reference_count")
                            if key in document_metadata
                        },
                    },
                )
            )
        return hits
