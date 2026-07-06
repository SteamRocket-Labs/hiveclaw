"""External attention router for activation candidates."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import re
from typing import Any

from app.runtime.activation_candidates import ActivationCandidate, ActivationHardMask, ActivationScore
from app.runtime.activation_query import ActivationQuery
from app.services.principal_context import PrincipalStack

ACTIVATION_ROUTER_OUTPUT_SCHEMA = "hive.ccplus.activation_router_output.v1"


def _text(value: Any, *, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def _string_tuple(value: Iterable[Any] | None) -> tuple[str, ...]:
    return tuple(text for item in value or () if (text := _text(item)))


def _first_feature(value: Any) -> str:
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, list | tuple | set | frozenset):
        for item in value:
            if text := _text(item):
                return text
    return _text(value)


def _terms(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {token for token in re.findall(r"[a-z0-9][a-z0-9_-]*", value.lower()) if token}
    if isinstance(value, Mapping):
        terms: set[str] = set()
        for child in value.values():
            terms.update(_terms(child))
        return terms
    if isinstance(value, Iterable):
        terms: set[str] = set()
        for child in value:
            terms.update(_terms(child))
        return terms
    return _terms(str(value))


def _feature_terms(candidate: ActivationCandidate, keys: tuple[str, ...]) -> set[str]:
    terms: set[str] = set()
    for key in keys:
        terms.update(_terms(candidate.key_features.get(key)))
    return terms


def _overlap_score(query_terms: set[str], candidate_terms: set[str]) -> float:
    if not query_terms:
        return 0.0
    return len(query_terms & candidate_terms) / len(query_terms)


def _candidate_field(candidate: ActivationCandidate, key: str, *, fallback: str = "") -> str:
    metadata_value = candidate.metadata.get(key)
    if text := _first_feature(metadata_value):
        return text
    return _first_feature(candidate.key_features.get(key)) or fallback


@dataclass(frozen=True, slots=True)
class ActivationRouterContext:
    principal_stack: PrincipalStack | None = None
    activation_query: ActivationQuery | Mapping[str, Any] | None = None
    allowed_candidate_kinds: tuple[str, ...] = ()
    denied_candidate_kinds: tuple[str, ...] = ()
    query_id: str = ""

    def __post_init__(self) -> None:
        query = self.activation_query
        if query is not None and not isinstance(query, ActivationQuery):
            query = ActivationQuery.from_manifest(query)
        object.__setattr__(self, "activation_query", query)
        object.__setattr__(self, "allowed_candidate_kinds", _string_tuple(self.allowed_candidate_kinds))
        object.__setattr__(self, "denied_candidate_kinds", _string_tuple(self.denied_candidate_kinds))
        query_id = _text(self.query_id) or (query.query_id if isinstance(query, ActivationQuery) else "")
        object.__setattr__(self, "query_id", query_id)


@dataclass(frozen=True, slots=True)
class ActivationRouterOutput:
    query_id: str = ""
    top_activation_candidates: tuple[ActivationCandidate, ...] = ()
    suppressed_activation_candidates: tuple[ActivationCandidate, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema": ACTIVATION_ROUTER_OUTPUT_SCHEMA,
            "query_id": self.query_id,
            "top_activation_candidates": [candidate.to_manifest() for candidate in self.top_activation_candidates],
            "suppressed_activation_candidates": [
                candidate.to_manifest() for candidate in self.suppressed_activation_candidates
            ],
            "metadata": dict(self.metadata),
        }


def _with_hard_mask(candidate: ActivationCandidate, hard_mask: ActivationHardMask) -> ActivationCandidate:
    manifest = candidate.to_manifest()
    manifest["hard_mask"] = hard_mask.to_manifest()
    return ActivationCandidate.from_manifest(manifest)


def _with_score(candidate: ActivationCandidate, score: ActivationScore) -> ActivationCandidate:
    manifest = candidate.to_manifest()
    manifest["score"] = score.to_manifest()
    return ActivationCandidate.from_manifest(manifest)


def _entity_terms(query: ActivationQuery) -> set[str]:
    terms: set[str] = set()
    for entity in query.entities:
        for key in ("name", "text", "value", "id"):
            terms.update(_terms(entity.get(key)))
    return terms


def _temporal_terms(query: ActivationQuery) -> set[str]:
    terms: set[str] = set()
    for hint in query.temporal_hints:
        for key in ("kind", "value", "label", "text"):
            terms.update(_terms(hint.get(key)))
    return terms


def _profile_terms(query: ActivationQuery) -> set[str]:
    terms: set[str] = set()
    for payload in (query.owner_context, query.task_profile):
        for key in ("profile_terms", "taste_terms", "preferences", "owner_preferences"):
            terms.update(_terms(payload.get(key)))
    return terms


def _multi_head_score(candidate: ActivationCandidate, query: ActivationQuery) -> ActivationScore:
    semantic_query_terms = _terms(query.concepts)
    semantic_candidate_terms = _feature_terms(
        candidate,
        ("concepts", "tags", "description_terms", "task_intent", "name", "title"),
    )
    entity_query_terms = _entity_terms(query)
    entity_candidate_terms = _feature_terms(candidate, ("entities", "entity", "name", "title"))
    temporal_query_terms = _temporal_terms(query)
    temporal_candidate_terms = _feature_terms(candidate, ("temporal_hints", "temporal", "recency"))
    profile_query_terms = _profile_terms(query)
    profile_candidate_terms = _feature_terms(candidate, ("profile_terms", "taste", "owner_preferences"))
    head_scores = {
        "semantic": _overlap_score(semantic_query_terms, semantic_candidate_terms),
        "entity": _overlap_score(entity_query_terms, entity_candidate_terms),
        "temporal": _overlap_score(temporal_query_terms, temporal_candidate_terms),
        "profile": _overlap_score(profile_query_terms, profile_candidate_terms),
    }
    weights = {"semantic": 0.4, "entity": 0.25, "temporal": 0.15, "profile": 0.2}
    reasons = tuple(f"{key}_match" for key, value in head_scores.items() if value > 0)
    return ActivationScore(
        head_scores=head_scores,
        weights=weights,
        reasons=reasons,
        scorer="activation_router_multi_head",
    )


def _policy_mask(candidate: ActivationCandidate, context: ActivationRouterContext) -> ActivationHardMask | None:
    kind = _text(candidate.candidate_kind)
    if context.allowed_candidate_kinds and kind not in context.allowed_candidate_kinds:
        return ActivationHardMask(
            allowed=False,
            reason="policy_denied",
            judge="activation_router",
            policy_ref="activation_router.allowed_candidate_kinds",
            details={"candidate_kind": kind},
        )
    if kind in context.denied_candidate_kinds:
        return ActivationHardMask(
            allowed=False,
            reason="policy_denied",
            judge="activation_router",
            policy_ref="activation_router.denied_candidate_kinds",
            details={"candidate_kind": kind},
        )
    return None


def _acl_mask(candidate: ActivationCandidate, principal_stack: PrincipalStack) -> ActivationHardMask | None:
    scope = _candidate_field(candidate, "acl_scope", fallback="company").lower()
    if scope in {"owner", "self", "private", "personal"} and not principal_stack.current_user_is_direct_owner:
        return ActivationHardMask(
            allowed=False,
            reason="acl_denied",
            judge="activation_router",
            policy_ref="activation_router.acl_scope",
            details={"acl_scope": scope},
        )
    if scope in {"company", "tenant"} and principal_stack.company is None:
        return ActivationHardMask(
            allowed=False,
            reason="acl_denied",
            judge="activation_router",
            policy_ref="activation_router.acl_scope",
            details={"acl_scope": scope},
        )
    return None


def _sensitivity_mask(candidate: ActivationCandidate, principal_stack: PrincipalStack) -> ActivationHardMask | None:
    sensitivity = _candidate_field(candidate, "sensitivity", fallback="PL1_public")
    if principal_stack.can_access_sensitivity(sensitivity):
        return None
    return ActivationHardMask(
        allowed=False,
        reason="sensitivity_denied",
        judge="activation_router",
        policy_ref="activation_router.sensitivity",
        details={"sensitivity": sensitivity},
    )


def route_activation_candidates(
    candidates: Iterable[ActivationCandidate],
    *,
    context: ActivationRouterContext,
) -> ActivationRouterOutput:
    principal_stack = context.principal_stack or PrincipalStack()
    top: list[ActivationCandidate] = []
    suppressed: list[ActivationCandidate] = []
    for candidate in candidates:
        hard_mask = (
            _policy_mask(candidate, context)
            or _acl_mask(candidate, principal_stack)
            or _sensitivity_mask(candidate, principal_stack)
        )
        if hard_mask is not None:
            suppressed.append(_with_hard_mask(candidate, hard_mask))
            continue
        if isinstance(context.activation_query, ActivationQuery):
            top.append(_with_score(candidate, _multi_head_score(candidate, context.activation_query)))
        else:
            top.append(candidate)
    if isinstance(context.activation_query, ActivationQuery):
        top = sorted(
            enumerate(top),
            key=lambda item: item[1].score.total_score if item[1].score else 0.0,
            reverse=True,
        )
        top_candidates = tuple(candidate for _index, candidate in top)
    else:
        top_candidates = tuple(top)
    return ActivationRouterOutput(
        query_id=context.query_id,
        top_activation_candidates=top_candidates,
        suppressed_activation_candidates=tuple(suppressed),
    )


__all__ = [
    "ACTIVATION_ROUTER_OUTPUT_SCHEMA",
    "ActivationRouterContext",
    "ActivationRouterOutput",
    "route_activation_candidates",
]
