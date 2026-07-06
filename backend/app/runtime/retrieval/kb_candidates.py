"""Personal / company knowledge-base activation candidate seam.

This module intentionally defines only the runtime contract. The default
provider returns no hits until Personal Knowledge / Company Knowledge services
attach a governed implementation behind the ACL context.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.runtime.activation_candidates import ActivationCandidate, ActivationScore, ActivationSurface
from app.runtime.context_candidates import build_metadata_activation_keys

KNOWLEDGE_BASE_SCOPES = frozenset({"personal", "company"})


def _clean_text(value: Any, *, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def _string_tuple(values: Iterable[Any] | None) -> tuple[str, ...]:
    return tuple(text for value in values or () if (text := _clean_text(value)))


def _string_list(values: Iterable[Any] | None) -> list[str]:
    return list(_string_tuple(values))


def _json_safe_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    result: dict[str, Any] = {}
    for key, child in value.items():
        if child is None or isinstance(child, str | int | float | bool):
            result[str(key)] = child
        elif isinstance(child, Mapping):
            result[str(key)] = _json_safe_mapping(child)
        elif isinstance(child, list | tuple | set | frozenset):
            result[str(key)] = [str(item) for item in child if _clean_text(item)]
        else:
            result[str(key)] = str(child)
    return result


@dataclass(frozen=True, slots=True)
class KnowledgeACLContext:
    tenant_id: Any
    agent_id: Any
    owner_user_id: Any
    current_user_id: Any
    allowed_scopes: tuple[str, ...] = ("personal", "company")

    def __post_init__(self) -> None:
        normalized = tuple(scope for scope in _string_tuple(self.allowed_scopes) if scope in KNOWLEDGE_BASE_SCOPES)
        object.__setattr__(self, "allowed_scopes", normalized)

    def allows_scope(self, scope: str) -> bool:
        normalized = _clean_text(scope)
        return normalized in KNOWLEDGE_BASE_SCOPES and normalized in self.allowed_scopes


@dataclass(frozen=True, slots=True)
class KnowledgeCandidateRecord:
    scope: str
    item_id: str
    title: str
    preview: str
    source_ref: str
    score: float = 0.0
    key_features: Mapping[str, Any] = field(default_factory=dict)
    value_pointer: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        scope = _clean_text(self.scope)
        if scope not in KNOWLEDGE_BASE_SCOPES:
            raise ValueError(f"unsupported knowledge scope: {scope!r}")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "item_id", _clean_text(self.item_id, fallback="item"))
        object.__setattr__(self, "title", _clean_text(self.title, fallback=self.item_id))
        object.__setattr__(self, "preview", _clean_text(self.preview))
        object.__setattr__(self, "source_ref", _clean_text(self.source_ref, fallback=f"knowledge://{scope}/{self.item_id}"))
        object.__setattr__(self, "score", float(self.score or 0.0))
        object.__setattr__(self, "key_features", _json_safe_mapping(self.key_features))
        object.__setattr__(self, "value_pointer", _json_safe_mapping(self.value_pointer))
        object.__setattr__(self, "metadata", _json_safe_mapping(self.metadata))


class KnowledgeCandidateProvider(Protocol):
    async def search(
        self,
        *,
        query: str,
        scope: str,
        acl_context: KnowledgeACLContext,
        limit: int,
    ) -> Sequence[KnowledgeCandidateRecord]:
        ...


class NullKnowledgeCandidateProvider:
    async def search(
        self,
        *,
        query: str,
        scope: str,
        acl_context: KnowledgeACLContext,
        limit: int,
    ) -> Sequence[KnowledgeCandidateRecord]:
        del query, scope, acl_context, limit
        return ()


def _record_to_activation_candidate(record: KnowledgeCandidateRecord, *, rank: int) -> ActivationCandidate:
    source_type = f"{record.scope}_knowledge_base"
    key_features = {
        "scope": [record.scope],
        "title": [record.title],
        "item_id": [record.item_id],
    }
    key_features.update(_json_safe_mapping(record.key_features))
    value_pointer = {
        "loader": "knowledge_base",
        "scope": record.scope,
        "item_id": record.item_id,
    }
    value_pointer.update(_json_safe_mapping(record.value_pointer))
    keys = build_metadata_activation_keys(
        candidate_kind="knowledge_base",
        item_id=f"{record.scope}:{record.item_id}",
        source_type=source_type,
        key_features=key_features,
        value_pointer=value_pointer,
        source_refs=[record.source_ref],
        ref_kind="knowledge_base",
        payload={
            "scope": record.scope,
            "item_id": record.item_id,
            "title": record.title,
            "source_ref": record.source_ref,
        },
    )
    score = max(0.0, min(1.0, record.score or (1.0 - (rank * 0.01))))
    return ActivationCandidate(
        candidate_kind="knowledge_base",
        candidate_ref=dict(keys["candidate_ref"]),
        key_features=dict(keys["key_features"]),
        value_pointer=dict(keys["value_pointer"]),
        surface=ActivationSurface(
            surface_kind="knowledge_base",
            preview=record.preview or record.title,
            token_estimate=max(1, len(record.preview or record.title) // 4),
            source_refs=(record.source_ref,),
        ),
        source_refs=(record.source_ref,),
        score=ActivationScore(
            head_scores={"knowledge_score": score},
            total_score=score,
            reasons=(f"{record.scope}_knowledge_hit",),
            scorer="knowledge_base_gatherer",
        ),
        metadata={
            "scope": record.scope,
            "item_id": record.item_id,
            "title": record.title,
            **dict(record.metadata),
        },
    )


async def gather_knowledge_base_candidates(
    query: str,
    *,
    acl_context: KnowledgeACLContext | None,
    provider: KnowledgeCandidateProvider | None = None,
    scopes: Iterable[str] = ("personal", "company"),
    limit: int = 10,
) -> list[ActivationCandidate]:
    if acl_context is None:
        raise ValueError("acl_context is required")
    clean_query = _clean_text(query)
    if not clean_query:
        return []

    candidate_provider = provider or NullKnowledgeCandidateProvider()
    candidates: list[ActivationCandidate] = []
    per_scope_limit = max(1, int(limit or 10))
    for scope in _string_list(scopes):
        if not acl_context.allows_scope(scope):
            continue
        rows = await candidate_provider.search(
            query=clean_query,
            scope=scope,
            acl_context=acl_context,
            limit=per_scope_limit,
        )
        for row in rows:
            if not acl_context.allows_scope(row.scope):
                continue
            candidates.append(_record_to_activation_candidate(row, rank=len(candidates)))
            if len(candidates) >= per_scope_limit:
                return candidates
    return candidates


__all__ = [
    "KNOWLEDGE_BASE_SCOPES",
    "KnowledgeACLContext",
    "KnowledgeCandidateProvider",
    "KnowledgeCandidateRecord",
    "NullKnowledgeCandidateProvider",
    "gather_knowledge_base_candidates",
]
