"""Dynamic activation scoring for memory candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.memory.types import MemoryItem
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
        if item.metadata.get("open_loop"):
            score += policy.open_loop_weight
            reasons.append("open_loop_pressure")
        retention_score = _float_meta(item, "retention_score")
        if retention_score > 0:
            score += retention_score * policy.retention_weight
            reasons.append("retention_score")
        if _float_meta(item, "confidence", default=0.0) >= 0.8:
            score += policy.confidence_weight
            reasons.append("confidence_weight")

        return ActivationDecision(item=item, score=round(min(score, 1.0), 4), reasons=reasons)


def _terms(text: str) -> set[str]:
    return {term for term in re.split(r"\W+", text.lower()) if term}


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
