"""Graph normalization, authority-aware search SQL, and ranking helpers."""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func, or_, select

from app.models.knowledge import KnowledgeDocument, KnowledgeSegment
from app.services.personal_knowledge_access import (
    PersonalKnowledgePrincipal,
    personal_knowledge_access_predicate,
    personal_knowledge_agent_visibility_predicate,
)

_WHITESPACE_RE = re.compile(r"\s+")


def clean_graph_text(value: Any, *, max_len: int | None = 300) -> str:
    clean = _WHITESPACE_RE.sub(" ", str(value or "").strip())
    if max_len is not None and len(clean) > max_len:
        raise ValueError(f"graph field exceeds the structural limit of {max_len} characters")
    return clean


def coerce_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(1.0, number))


def merge_unique_strings(
    existing: list[Any] | tuple[Any, ...] | None,
    additions: list[Any] | tuple[Any, ...],
) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in [*(existing or []), *additions]:
        clean = clean_graph_text(value)
        key = clean.lower()
        if not clean or key in seen:
            continue
        seen.add(key)
        merged.append(clean)
    return merged


def merge_source_refs(
    existing: list[Any] | tuple[Any, ...] | None,
    additions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
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


def escape_like(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_personal_knowledge_search_statement(
    *,
    tenant_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    query: str,
    principal: PersonalKnowledgePrincipal,
    limit: int | None,
):
    clean_query = _WHITESPACE_RE.sub(" ", str(query or "").strip())
    if not clean_query:
        raise ValueError("query must not be empty")

    ts_query = func.plainto_tsquery("simple", clean_query)
    score = func.coalesce(func.ts_rank_cd(KnowledgeSegment.tsv, ts_query), 0.0).label("score")
    like_query = f"%{escape_like(clean_query)}%"
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
            KnowledgeDocument.status.in_(("ready", "degraded")),
            personal_knowledge_agent_visibility_predicate(
                principal=principal,
            ),
            KnowledgeSegment.tenant_id == tenant_id,
            KnowledgeSegment.scope_type == "person",
            KnowledgeSegment.scope_id == owner_user_id,
            search_predicate,
            personal_knowledge_access_predicate(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                principal=principal,
                action="search",
            ),
        )
        .order_by(desc(score), KnowledgeDocument.updated_at.desc(), KnowledgeSegment.position.asc())
    )
    if limit is not None:
        statement = statement.limit(max(1, int(limit)))
    return statement


def row_first(row: Any) -> Any:
    if isinstance(row, tuple):
        return row[0]
    try:
        return row[0]
    except (TypeError, KeyError, IndexError):
        return row


def source_ref_segment_ids(refs: Any) -> list[uuid.UUID]:
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


def freshness_boost(updated_at: Any) -> float:
    if not isinstance(updated_at, datetime):
        return 0.0
    timestamp = updated_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds() / 86400.0)
    return min(0.03, 0.03 / (1.0 + age_days / 30.0))


def heat_boost(metadata: dict[str, Any]) -> float:
    raw = metadata.get("citation_count", metadata.get("usage_count", metadata.get("reference_count", 0)))
    try:
        count = max(0.0, float(raw or 0.0))
    except (TypeError, ValueError):
        return 0.0
    return min(0.05, math.log1p(count) * 0.01)


_clean_graph_text = clean_graph_text
_coerce_confidence = coerce_confidence
_merge_unique_strings = merge_unique_strings
_merge_source_refs = merge_source_refs
_escape_like = escape_like
_row_first = row_first
_source_ref_segment_ids = source_ref_segment_ids
_freshness_boost = freshness_boost
_heat_boost = heat_boost
