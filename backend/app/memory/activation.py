"""Dynamic activation scoring for memory candidates."""

from __future__ import annotations

import math
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
    # Injectable clock for deterministic scoring (evals, replay); None → wall
    # clock. BaseLevel decay is the only time-dependent term.
    now: datetime | None = None
    # Session working set W_t (design §4.2): (ref, strength) pointers to the
    # memories this session already activated. Pointers only — never bodies.
    working_set: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True, slots=True)
class ActivationPolicy:
    goal_weight: float = 0.25
    owner_weight: float = 0.2
    company_weight: float = 0.15
    open_loop_weight: float = 0.2
    retention_weight: float = 0.2
    confidence_weight: float = 0.05
    # BaseLevel (design §4.3) replaces the old tie-break-only usage heat:
    # frequency + power-law recency + feedback credit, promoted to a real
    # (but still bounded, non-hijacking) weight alongside goal/owner pressure.
    base_level_weight: float = 0.2
    # ContextBoost (design §4.2): session-working-set diffusion over the
    # relation graph. Small and bounded — context warms neighbors, it never
    # hijacks literal relevance.
    context_boost_weight: float = 0.15


@dataclass(frozen=True, slots=True)
class ActivationDecision:
    item: MemoryItem
    score: float
    reasons: list[str]
    suppressed: bool = False
    # Unclamped ranking score: `score` saturates at 1.0 for display/budget
    # contracts, which would collapse ordering between highly-relevant items;
    # rankers must sort on this instead.
    raw_score: float = 0.0


class ActivationScorer:
    def __init__(self, policy: ActivationPolicy | None = None) -> None:
        self.policy = policy or ActivationPolicy()

    def score(self, item: MemoryItem, context: ActivationContext) -> ActivationDecision:
        lifecycle_suppression = memory_lifecycle_suppression_reason(item.metadata)
        if lifecycle_suppression:
            return ActivationDecision(
                item=item, score=0.0, reasons=[lifecycle_suppression], suppressed=True, raw_score=0.0
            )

        sensitivity = str(item.metadata.get("sensitivity", "PL1_public"))
        if not context.principal_stack.can_access_sensitivity(sensitivity):
            return ActivationDecision(
                item=item, score=0.0, reasons=["sensitivity_strip"], suppressed=True, raw_score=0.0
            )

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
        base_level = _base_level(item, now=context.now)
        if base_level > 0:
            score += base_level * policy.base_level_weight
            reasons.append("base_level")
        context_boost = _float_meta(item, "context_boost")
        if context_boost > 0:
            score += min(1.0, context_boost) * policy.context_boost_weight
            reasons.append("context_boost")

        return ActivationDecision(
            item=item,
            score=round(min(score, 1.0), 4),
            reasons=reasons,
            raw_score=round(score, 4),
        )


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


# ACT-R style power-law decay exponent (design §4.3): strength of one access
# aged `t` hours contributes t^(-d). d=0.5 is the classic memory-decay value.
_BASE_LEVEL_DECAY = 0.5
# Minimum age (hours) per access — prevents t^(-d) blow-up for just-touched
# entries; one minute is well below any real inter-turn gap.
_BASE_LEVEL_MIN_AGE_HOURS = 1.0 / 60.0
# Log-saturation reference: raw strength at which BaseLevel reaches 1.0.
# A full K=8 ring accessed within the last hour sums to ~8.0, so ~20 leaves
# clear headroom for credit while keeping normal usage in the responsive zone.
_BASE_LEVEL_SATURATION = 20.0


def _base_level(item: MemoryItem, *, now: datetime | None = None) -> float:
    """Bounded frequency + power-law recency + feedback credit (design §4.3).

    ``BaseLevel = min(1, ln(1 + Σ t_j^(-d) + credit) / ln(1 + saturation))``
    fed by the lifecycle-sidecar telemetry joined onto item metadata:
    ``recent_accesses`` (K-ring of ISO timestamps; falls back to the legacy
    single ``last_accessed`` point so pre-ring sidecar data keeps working)
    and ``credit`` (owner-feedback reinforcement, may be negative).
    """
    raw_ring = item.metadata.get("recent_accesses")
    moments: list[datetime] = []
    if isinstance(raw_ring, list | tuple):
        for value in raw_ring:
            parsed = parse_utc_timestamp(str(value or ""))
            if parsed is not None:
                moments.append(parsed)
    if not moments:
        raw_last_accessed = str(item.metadata.get("last_accessed") or "").strip()
        if raw_last_accessed and raw_last_accessed.lower() != "never":
            parsed = parse_utc_timestamp(raw_last_accessed)
            if parsed is not None:
                moments.append(parsed)

    credit = _float_meta(item, "credit")
    if not moments and credit == 0.0:
        return 0.0

    reference = now or datetime.now(UTC)
    strength = 0.0
    for moment in moments:
        age_hours = max((reference - moment).total_seconds() / 3600.0, _BASE_LEVEL_MIN_AGE_HOURS)
        strength += age_hours**-_BASE_LEVEL_DECAY
    raw = strength + credit
    if raw <= 0.0:
        return 0.0
    normalized = math.log1p(raw) / math.log1p(_BASE_LEVEL_SATURATION)
    return round(min(1.0, normalized), 4)
