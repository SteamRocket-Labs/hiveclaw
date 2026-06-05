"""PromotionRouter — single routing gate for memory promotion signals.

Spec: docs/agent-memory-md-first-spec.md §6. Distillers produce candidates;
this router decides which container lane a signal becomes a candidate for.
One signal becomes exactly ONE candidate — never T3 strategy, SKILL.md, and
workflow definition simultaneously.

AI-native routing rule (anti-Mem0-V3):

    exact rule match + high confidence + no conflicting signals
      -> deterministic candidate (fast path, pure code)
    ambiguous / low confidence / cross-container / evidence-conflicting
      -> escalate to injected LLM adjudication
    adjudicator absent, failing, or out-of-bounds
      -> HELD candidate with audit reason; no durable target write

Pure module: zero IO, zero LLM-client imports. The adjudicator is an injected
async callable so callers wire the tenant's model; tests inject fakes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

from app.memory.types import CONTAINER_CANDIDATES

logger = logging.getLogger(__name__)

# Repetition threshold for identity-level promotion (engineering rule, not
# neuroscience truth — spec §1). Matches dream's 3-confirmation convention.
_SOUL_REPEAT_THRESHOLD = 3
# Durable-container promotions below this confidence are semantic calls.
_DURABLE_CONFIDENCE_FLOOR = 0.6


class PromotionKind(str, Enum):
    MEMORY_APPEND = "memory_append"
    SOUL_CANDIDATE = "soul_candidate"
    SKILL_CANDIDATE = "skill_candidate"
    WORKFLOW_CANDIDATE = "workflow_candidate"
    ARTIFACT_ONLY = "artifact_only"
    LIFECYCLE_PATCH = "lifecycle_patch"


# Kinds whose target containers are high-impact and therefore require
# evidence (source refs) before any candidate is produced.
_DURABLE_KINDS = frozenset(
    {PromotionKind.SOUL_CANDIDATE, PromotionKind.SKILL_CANDIDATE, PromotionKind.WORKFLOW_CANDIDATE}
)


class RouteStatus(str, Enum):
    ROUTED = "routed"
    NEEDS_ADJUDICATION = "needs_adjudication"
    HELD = "held"


@dataclass(slots=True)
class PromotionSignal:
    """One distilled signal awaiting container routing."""

    category: str
    content: str
    source_refs: list[str]
    evidence: str
    confidence: float
    scope: str
    concept: str | None = None
    container_hint: str | None = None
    repeat_count: int = 1
    explicit: bool = False
    repeated_success: bool = False
    has_durable_state: bool = False
    runtime_only: bool = False
    supersedes: list[str] = field(default_factory=list)
    contradicts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PromotionCandidate:
    """Spec §6 candidate shape — the router's only durable output."""

    kind: PromotionKind
    content: str
    source_refs: list[str]
    evidence: str
    confidence: float
    scope: str
    concept: str | None = None
    reason: str = ""


@dataclass(slots=True)
class PromotionAdjudication:
    """LLM adjudicator verdict for an escalated signal."""

    kind: PromotionKind
    confidence: float
    reason: str


@dataclass(slots=True)
class RoutedPromotion:
    status: RouteStatus
    candidate: PromotionCandidate | None
    reason: str
    allowed_kinds: list[PromotionKind] = field(default_factory=list)
    audit: dict[str, object] = field(default_factory=dict)


AdjudicatorFn = Callable[[PromotionSignal, list[PromotionKind]], Awaitable[PromotionAdjudication]]


def _candidate(signal: PromotionSignal, kind: PromotionKind, reason: str) -> PromotionCandidate:
    return PromotionCandidate(
        kind=kind,
        content=signal.content,
        source_refs=list(signal.source_refs),
        evidence=signal.evidence,
        confidence=signal.confidence,
        scope=signal.scope,
        concept=signal.concept,
        reason=reason,
    )


def _routed(signal: PromotionSignal, kind: PromotionKind, reason: str, *, rule: str) -> RoutedPromotion:
    # Durable containers demand evidence — a soul/skill/workflow candidate
    # without source refs is unaccountable and becomes a HELD audit record.
    if kind in _DURABLE_KINDS and not signal.source_refs:
        return RoutedPromotion(
            status=RouteStatus.HELD,
            candidate=None,
            reason=f"{kind.value} requires source_refs; none provided",
            audit={"rule": rule, "held": "missing_source_refs"},
        )
    return RoutedPromotion(
        status=RouteStatus.ROUTED,
        candidate=_candidate(signal, kind, reason),
        reason=reason,
        audit={"rule": rule},
    )


def _escalation(allowed: list[PromotionKind], reason: str) -> RoutedPromotion:
    return RoutedPromotion(
        status=RouteStatus.NEEDS_ADJUDICATION,
        candidate=None,
        reason=reason,
        allowed_kinds=allowed,
        audit={"escalation_reason": reason},
    )


def _rule_kind_for_category(signal: PromotionSignal) -> tuple[PromotionKind, str, str]:
    """Deterministic category mapping (spec §6 first-pass routing rules)."""
    category = (signal.category or "general").strip().lower()

    if category in {"feedback", "constraint"}:
        if signal.explicit or signal.repeat_count >= _SOUL_REPEAT_THRESHOLD:
            return (
                PromotionKind.SOUL_CANDIDATE,
                f"{category} with {'explicit owner instruction' if signal.explicit else f'{signal.repeat_count} repetitions'}",
                "feedback_repeated_or_explicit",
            )
        return (
            PromotionKind.MEMORY_APPEND,
            f"{category} without repeated/explicit signal stays memory",
            "feedback_single",
        )

    if category == "blocked_pattern":
        return (PromotionKind.MEMORY_APPEND, "blocked pattern recorded as durable memory", "blocked_pattern")

    if category == "strategy":
        if signal.repeated_success and signal.has_durable_state:
            return (
                PromotionKind.WORKFLOW_CANDIDATE,
                "repeated strategy requiring durable state / replay / gates",
                "strategy_stateful",
            )
        if signal.repeated_success:
            return (
                PromotionKind.SKILL_CANDIDATE,
                "repeated successful strategy with no durable state",
                "strategy_stateless",
            )
        return (PromotionKind.MEMORY_APPEND, "single-occurrence strategy stays memory evidence", "strategy_single")

    # project / reference / general / user / error / request and unknown.
    return (PromotionKind.MEMORY_APPEND, f"{category} routes to durable memory", "plain_memory_category")


def fast_path_route(signal: PromotionSignal) -> RoutedPromotion:
    """Deterministic first pass. Pure code — never guesses on semantic calls.

    Returns ROUTED for exact rule matches, NEEDS_ADJUDICATION for ambiguity
    (cross-container hints, hint/rule conflicts, low-confidence durable
    promotions), HELD for evidence violations.
    """
    # 1. Runtime-only evidence never becomes durable memory.
    if signal.runtime_only:
        return _routed(signal, PromotionKind.ARTIFACT_ONLY, "runtime-only evidence", rule="runtime_only")

    # 2. Contradiction / supersession is a lifecycle state change, not new truth.
    if signal.contradicts or signal.supersedes:
        edge = "contradicts" if signal.contradicts else "supersedes"
        return _routed(
            signal,
            PromotionKind.LIFECYCLE_PATCH,
            f"signal {edge} existing entries — lifecycle patch lane",
            rule="lifecycle_edge",
        )

    rule_kind, rule_reason, rule_name = _rule_kind_for_category(signal)

    # 3. Container hints are advisory evidence; a hint that disagrees with
    #    the deterministic rule is a semantic call → escalate, never pick
    #    mechanically between soul/skill/workflow (anti-Mem0-V3).
    hint = (signal.container_hint or "").strip().lower()
    if hint and hint in CONTAINER_CANDIDATES:
        hint_kind = PromotionKind(hint)
        if hint_kind is not rule_kind:
            allowed = sorted({rule_kind, hint_kind}, key=lambda kind: kind.value)
            return _escalation(
                allowed,
                f"container hint '{hint}' conflicts with rule '{rule_name}' → {rule_kind.value}",
            )

    # 4. Low-confidence durable promotion is a semantic call, not a rule hit.
    if rule_kind in _DURABLE_KINDS and signal.confidence < _DURABLE_CONFIDENCE_FLOOR:
        return _escalation(
            [rule_kind, PromotionKind.MEMORY_APPEND],
            f"confidence {signal.confidence:.2f} below durable floor {_DURABLE_CONFIDENCE_FLOOR} for {rule_kind.value}",
        )

    return _routed(signal, rule_kind, rule_reason, rule=rule_name)


async def route_promotion_signal(
    signal: PromotionSignal,
    *,
    adjudicator: AdjudicatorFn | None = None,
) -> RoutedPromotion:
    """Full route: deterministic fast path, then LLM adjudication on ambiguity.

    The adjudicator is the semantic decision owner for escalations. When it is
    absent, fails, or answers outside the allowed kinds, the signal is HELD
    with an audit reason — no durable target write happens on a broken path.
    """
    routed = fast_path_route(signal)
    if routed.status is not RouteStatus.NEEDS_ADJUDICATION:
        return routed

    if adjudicator is None:
        return RoutedPromotion(
            status=RouteStatus.HELD,
            candidate=None,
            reason=f"held: {routed.reason} (no adjudicator available)",
            allowed_kinds=routed.allowed_kinds,
            audit={**routed.audit, "held": "no_adjudicator"},
        )

    try:
        verdict = await adjudicator(signal, list(routed.allowed_kinds))
    except Exception as exc:  # noqa: BLE001 — any adjudicator failure must hold, not crash the pipeline
        logger.warning("[PromotionRouter] adjudicator failed for category=%s: %s", signal.category, exc)
        return RoutedPromotion(
            status=RouteStatus.HELD,
            candidate=None,
            reason=f"held: adjudicator failed ({type(exc).__name__})",
            allowed_kinds=routed.allowed_kinds,
            audit={**routed.audit, "held": "adjudicator_error", "adjudicator_error": f"{type(exc).__name__}: {exc}"},
        )

    if not isinstance(verdict, PromotionAdjudication) or verdict.kind not in routed.allowed_kinds:
        bad_kind = getattr(verdict, "kind", None)
        logger.warning(
            "[PromotionRouter] adjudicator verdict out of bounds: %s not in %s",
            bad_kind,
            [kind.value for kind in routed.allowed_kinds],
        )
        return RoutedPromotion(
            status=RouteStatus.HELD,
            candidate=None,
            reason="held: adjudicator verdict outside allowed kinds",
            allowed_kinds=routed.allowed_kinds,
            audit={**routed.audit, "held": "verdict_out_of_bounds", "verdict_kind": str(bad_kind)},
        )

    final = _routed(signal, verdict.kind, verdict.reason, rule="adjudicated")
    if final.status is RouteStatus.ROUTED and final.candidate is not None:
        final.candidate.confidence = max(0.0, min(1.0, verdict.confidence))
        final.audit = {**routed.audit, "adjudicated": True, "rule": "adjudicated"}
    return final
