"""Dynamic activation scoring for memory candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.memory.types import MemoryItem, parse_utc_timestamp
from app.services.principal_context import PrincipalStack


@dataclass(frozen=True, slots=True)
class ActivationContext:
    query: str
    principal_stack: PrincipalStack
    goal_terms: list[str] = field(default_factory=list)
    owner_terms: list[str] = field(default_factory=list)
    company_terms: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ActivationPolicy:
    goal_weight: float = 0.25
    owner_weight: float = 0.2
    company_weight: float = 0.15
    open_loop_weight: float = 0.2
    retention_weight: float = 0.2
    confidence_weight: float = 0.05
    usage_heat_weight: float = 0.05


@dataclass(frozen=True, slots=True)
class ActivationDecision:
    item: MemoryItem
    score: float
    reasons: list[str]
    suppressed: bool = False


class ActivationScorer:
    def __init__(self, policy: ActivationPolicy | None = None) -> None:
        self.policy = policy or ActivationPolicy()

    def score(self, item: MemoryItem, context: ActivationContext) -> ActivationDecision:
        lifecycle_suppression = memory_lifecycle_suppression_reason(item.metadata)
        if lifecycle_suppression:
            return ActivationDecision(item=item, score=0.0, reasons=[lifecycle_suppression], suppressed=True)

        sensitivity = str(item.metadata.get("sensitivity", "PL1_public"))
        if not context.principal_stack.can_access_sensitivity(sensitivity):
            return ActivationDecision(item=item, score=0.0, reasons=["sensitivity_strip"], suppressed=True)

        policy = self.policy
        score = float(item.score)
        reasons: list[str] = []
        content = item.content
        query_terms = _terms(context.query)

        if _overlap(query_terms | set(context.goal_terms), content):
            score += policy.goal_weight
            reasons.append("goal_relevance")
        if _overlap(set(context.owner_terms) | {"owner"}, content):
            score += policy.owner_weight
            reasons.append("principal_relevance")
        if _overlap(set(context.company_terms), content):
            score += policy.company_weight
            reasons.append("company_relevance")
        if _bool_meta(item, "open_loop"):
            score += policy.open_loop_weight
            reasons.append("open_loop_pressure")
        retention_score = _float_meta(item, "retention_score")
        if retention_score > 0:
            score += retention_score * policy.retention_weight
            reasons.append("retention_score")
        if _float_meta_any(item, ("confidence", "conf"), default=0.0) >= 0.8:
            score += policy.confidence_weight
            reasons.append("confidence_weight")
        usage_heat = _usage_heat(item)
        if usage_heat > 0:
            score += usage_heat * policy.usage_heat_weight
            reasons.append("usage_heat")

        return ActivationDecision(item=item, score=round(min(score, 1.0), 4), reasons=reasons)


def _terms(text: str) -> set[str]:
    return {term for term in re.split(r"\W+", text.lower()) if term}


def memory_lifecycle_suppression_reason(metadata: dict) -> str:
    ttl_status = str(metadata.get("ttl_status") or "").strip().lower()
    if ttl_status == "expired" or _bool_meta_dict(metadata, "expired"):
        return "memory_ttl_expired"

    conflict_status = str(metadata.get("conflict_status") or "").strip().lower()
    if conflict_status in {"needs_review", "unresolved", "conflicted"} or _bool_meta_dict(metadata, "memory_conflict"):
        return "memory_conflict_unresolved"

    reference_status = str(metadata.get("reference_status") or "").strip().lower()
    if reference_status in {"invalid", "revalidation_required", "expired", "stale"}:
        return "memory_reference_revalidation_required"
    if _bool_meta_dict(metadata, "needs_revalidation"):
        return "memory_reference_revalidation_required"
    return ""


def _overlap(needles: set[str], haystack: str) -> bool:
    if not needles:
        return False
    haystack_terms = _terms(haystack)
    return bool({needle.lower() for needle in needles if needle} & haystack_terms)


def _float_meta(item: MemoryItem, key: str, default: float = 0.0) -> float:
    try:
        return float(item.metadata.get(key, default))
    except (TypeError, ValueError):
        return default


def _float_meta_any(item: MemoryItem, keys: tuple[str, ...], default: float = 0.0) -> float:
    for key in keys:
        if key in item.metadata:
            return _float_meta(item, key, default=default)
    return default


def _bool_meta(item: MemoryItem, key: str) -> bool:
    return _bool_meta_dict(item.metadata, key)


def _bool_meta_dict(metadata: dict, key: str) -> bool:
    value = metadata.get(key)
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    normalized = str(value).strip().lower()
    return normalized in {"1", "true", "yes", "y", "on"}


def _usage_heat(item: MemoryItem) -> float:
    """Bounded recall heat from sidecar telemetry.

    The signal is intentionally small: access telemetry can break ties and keep
    useful memories alive, but cannot replace literal relevance, owner/company
    pressure, or explicit retention policy.
    """
    access_count = max(0.0, _float_meta(item, "access_count"))
    count_score = min(access_count, 10.0) / 10.0

    recency_score = 0.0
    raw_last_accessed = str(item.metadata.get("last_accessed") or "").strip()
    if raw_last_accessed and raw_last_accessed.lower() != "never":
        accessed_at = parse_utc_timestamp(raw_last_accessed)
        if accessed_at is not None:
            age_days = max(0.0, (datetime.now(UTC) - accessed_at).total_seconds() / 86400)
            if age_days <= 7:
                recency_score = 1.0
            elif age_days <= 30:
                recency_score = 0.5

    heat = (0.7 * count_score) + (0.3 * recency_score)
    return round(min(1.0, max(0.0, heat)), 4)
