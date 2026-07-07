"""Personal Knowledge Base ingestion and search primitives."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, desc, exists, func, or_, select, update

from app.config import get_settings
from app.models.knowledge import KnowledgeDocument, KnowledgeGrant, KnowledgeIndexJob, KnowledgeSegment


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_WHITESPACE_RE = re.compile(r"\s+")


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
    source_sha256: str
    artifact_hash: str
    canonical_md_path: str
    segment_count: int
    status: str


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


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def _escape_like(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
    statement = (
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
        )
        .order_by(desc(score), KnowledgeDocument.updated_at.desc(), KnowledgeSegment.position.asc())
        .limit(max(1, int(limit or 5)))
    )
    if current_user_id == owner_user_id:
        return statement

    grantee_predicates = []
    if current_user_id is not None:
        grantee_predicates.append(
            and_(KnowledgeGrant.grantee_type == "user", KnowledgeGrant.grantee_id == current_user_id)
        )
    if agent_id is not None:
        grantee_predicates.append(and_(KnowledgeGrant.grantee_type == "agent", KnowledgeGrant.grantee_id == agent_id))
    if not grantee_predicates:
        return statement.where(False)

    grant_exists = exists(
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
    return statement.where(grant_exists)


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

    def __init__(self, *, data_root: str | Path | None = None) -> None:
        self.data_root = Path(data_root) if data_root is not None else Path(get_settings().AGENT_DATA_DIR)

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
    ) -> PersonalKnowledgeIngestResult:
        canonical_md = _normalize_markdown(markdown)
        if not canonical_md:
            raise ValueError("markdown must not be empty")

        clean_title = _clean_title(title)
        clean_source_kind = _clean_title(source_kind).lower().replace(" ", "_")
        source_payload = "\n".join([clean_source_kind, source_uri or "", canonical_md])
        source_sha256 = _sha256(source_payload)
        artifact_hash = _sha256(canonical_md)
        artifact_path = personal_knowledge_artifact_path(self.data_root, owner_user_id, source_sha256)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(canonical_md, encoding="utf-8")
        canonical_md_path = artifact_path.relative_to(self.data_root).as_posix()

        existing_result = await session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.scope_type == "person",
                KnowledgeDocument.scope_id == owner_user_id,
                KnowledgeDocument.source_sha256 == source_sha256,
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
                source_sha256=source_sha256,
                artifact_hash=artifact_hash,
                title=clean_title,
                status="ready",
                sensitivity=sensitivity,
                agent_searchable=agent_searchable,
                canonical_md_path=canonical_md_path,
                canonical_md_sha256=artifact_hash,
                doc_metadata_json={"ingest_format": "canonical_markdown"},
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
            document.doc_metadata_json = {**(document.doc_metadata_json or {}), "ingest_format": "canonical_markdown"}
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

        if previous_artifact_hash != artifact_hash:
            session.add(
                KnowledgeIndexJob(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    document_id=document.id,
                    scope_type="person",
                    scope_id=owner_user_id,
                    artifact_hash=artifact_hash,
                    stage="indexed",
                    status="ready",
                    attempt_count=1,
                    job_metadata_json={"channels": ["tsvector", "segments"]},
                )
            )
        await session.flush()

        await session.execute(
            update(KnowledgeSegment)
            .where(KnowledgeSegment.document_id == document.id)
            .values(tsv=func.to_tsvector("simple", KnowledgeSegment.content))
        )

        return PersonalKnowledgeIngestResult(
            document_id=document.id,
            source_sha256=source_sha256,
            artifact_hash=artifact_hash,
            canonical_md_path=canonical_md_path,
            segment_count=len(segment_drafts),
            status="ready",
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
