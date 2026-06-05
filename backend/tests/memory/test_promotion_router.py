"""PromotionRouter tests (docs/agent-memory-md-first-spec.md §6 / §12 P1).

Acceptance:
- Same input cannot route to skill and workflow simultaneously.
- Every route has source refs, confidence, and reason.
- Runtime artifacts can be classified as artifact-only.

AI-native invariant (anti-Mem0-V3): mechanical routing must never silently
choose between soul/skill/workflow when evidence is semantic or ambiguous —
those escalate to LLM adjudication or become HELD audit records.
"""

from __future__ import annotations

import pytest

from app.memory.promotion_router import (
    PromotionAdjudication,
    PromotionCandidate,
    PromotionKind,
    PromotionSignal,
    RouteStatus,
    fast_path_route,
    route_promotion_signal,
)


def _signal(**overrides) -> PromotionSignal:
    base = dict(
        category="reference",
        content="Hive backend dev server listens on port 8008",
        source_refs=["t2:learnings/insights.md#entry:abc123"],
        evidence="user_stated",
        confidence=0.9,
        scope="agent",
    )
    base.update(overrides)
    return PromotionSignal(**base)


# ── Deterministic fast-path: unambiguous category routing ──


def test_plain_knowledge_categories_route_to_memory_append() -> None:
    for category in ("project", "reference", "general", "user"):
        routed = fast_path_route(_signal(category=category))
        assert routed.status is RouteStatus.ROUTED
        assert routed.candidate is not None
        assert routed.candidate.kind is PromotionKind.MEMORY_APPEND
        assert routed.candidate.reason


def test_feedback_without_repetition_routes_to_memory_append() -> None:
    routed = fast_path_route(_signal(category="feedback", repeat_count=1, explicit=False))
    assert routed.status is RouteStatus.ROUTED
    assert routed.candidate.kind is PromotionKind.MEMORY_APPEND


def test_repeated_feedback_routes_to_soul_candidate() -> None:
    routed = fast_path_route(_signal(category="feedback", repeat_count=3))
    assert routed.status is RouteStatus.ROUTED
    assert routed.candidate.kind is PromotionKind.SOUL_CANDIDATE


def test_explicit_constraint_routes_to_soul_candidate() -> None:
    routed = fast_path_route(_signal(category="constraint", explicit=True))
    assert routed.status is RouteStatus.ROUTED
    assert routed.candidate.kind is PromotionKind.SOUL_CANDIDATE


def test_blocked_pattern_routes_to_memory_append() -> None:
    routed = fast_path_route(_signal(category="blocked_pattern"))
    assert routed.status is RouteStatus.ROUTED
    assert routed.candidate.kind is PromotionKind.MEMORY_APPEND


def test_contradiction_signal_routes_to_lifecycle_patch() -> None:
    routed = fast_path_route(_signal(category="blocked_pattern", contradicts=["mem_old123"]))
    assert routed.status is RouteStatus.ROUTED
    assert routed.candidate.kind is PromotionKind.LIFECYCLE_PATCH


def test_duplicate_signal_routes_to_lifecycle_patch() -> None:
    routed = fast_path_route(_signal(category="reference", supersedes=["mem_dup456"]))
    assert routed.status is RouteStatus.ROUTED
    assert routed.candidate.kind is PromotionKind.LIFECYCLE_PATCH


def test_runtime_only_evidence_routes_to_artifact_only() -> None:
    routed = fast_path_route(_signal(category="reference", runtime_only=True, source_refs=[], confidence=0.5))
    assert routed.status is RouteStatus.ROUTED
    assert routed.candidate.kind is PromotionKind.ARTIFACT_ONLY


def test_repeated_stateless_strategy_routes_to_skill_candidate() -> None:
    routed = fast_path_route(
        _signal(category="strategy", repeat_count=3, repeated_success=True, has_durable_state=False)
    )
    assert routed.status is RouteStatus.ROUTED
    assert routed.candidate.kind is PromotionKind.SKILL_CANDIDATE


def test_stateful_strategy_routes_to_workflow_candidate() -> None:
    routed = fast_path_route(
        _signal(category="strategy", repeat_count=3, repeated_success=True, has_durable_state=True)
    )
    assert routed.status is RouteStatus.ROUTED
    assert routed.candidate.kind is PromotionKind.WORKFLOW_CANDIDATE


def test_single_occurrence_strategy_stays_memory_append() -> None:
    routed = fast_path_route(_signal(category="strategy", repeat_count=1, repeated_success=False))
    assert routed.status is RouteStatus.ROUTED
    assert routed.candidate.kind is PromotionKind.MEMORY_APPEND


# ── Acceptance: one signal → exactly one candidate (never skill AND workflow) ──


def test_route_returns_single_candidate_never_skill_and_workflow() -> None:
    # The same input cannot be routed to skill and workflow simultaneously:
    # route output is a single candidate (or an escalation), never a list.
    routed = fast_path_route(
        _signal(category="strategy", repeat_count=3, repeated_success=True, has_durable_state=True)
    )
    assert routed.candidate is not None  # singular by construction
    assert routed.candidate.kind in (PromotionKind.SKILL_CANDIDATE, PromotionKind.WORKFLOW_CANDIDATE)


def test_every_routed_candidate_carries_refs_confidence_reason() -> None:
    cases = [
        _signal(category="reference"),
        _signal(category="feedback", repeat_count=3),
        _signal(category="strategy", repeat_count=3, repeated_success=True, has_durable_state=False),
        _signal(category="blocked_pattern", contradicts=["mem_x"]),
    ]
    for signal in cases:
        routed = fast_path_route(signal)
        assert routed.status is RouteStatus.ROUTED
        candidate = routed.candidate
        assert isinstance(candidate, PromotionCandidate)
        assert candidate.source_refs == signal.source_refs
        assert 0.0 <= candidate.confidence <= 1.0
        assert candidate.reason.strip()


# ── Anti-Mem0-V3: ambiguity escalates, never silently resolved mechanically ──


def test_conflicting_container_hint_escalates() -> None:
    # Extractor hinted skill, but the evidence says durable state → workflow.
    routed = fast_path_route(
        _signal(
            category="strategy",
            repeat_count=3,
            repeated_success=True,
            has_durable_state=True,
            container_hint="skill_candidate",
        )
    )
    assert routed.status is RouteStatus.NEEDS_ADJUDICATION
    assert routed.candidate is None
    assert "hint" in routed.reason.lower() or "conflict" in routed.reason.lower()


def test_low_confidence_durable_promotion_escalates() -> None:
    routed = fast_path_route(_signal(category="feedback", repeat_count=3, confidence=0.4))
    assert routed.status is RouteStatus.NEEDS_ADJUDICATION


def test_cross_container_hint_on_plain_category_escalates() -> None:
    # reference content hinted as soul material — semantic call, not mechanical.
    routed = fast_path_route(_signal(category="reference", container_hint="soul_candidate"))
    assert routed.status is RouteStatus.NEEDS_ADJUDICATION


def test_durable_candidate_without_source_refs_is_held() -> None:
    routed = fast_path_route(_signal(category="feedback", repeat_count=3, source_refs=[]))
    assert routed.status is RouteStatus.HELD
    assert routed.candidate is None
    assert "source_refs" in routed.reason or "source refs" in routed.reason


# ── Adjudication wiring (LLM injected; pure module never imports a client) ──


@pytest.mark.asyncio
async def test_adjudicator_resolves_escalation() -> None:
    async def adjudicator(signal: PromotionSignal, allowed: list[PromotionKind]) -> PromotionAdjudication:
        return PromotionAdjudication(
            kind=PromotionKind.WORKFLOW_CANDIDATE,
            confidence=0.85,
            reason="durable state + replay gates outweigh the skill hint",
        )

    signal = _signal(
        category="strategy",
        repeat_count=3,
        repeated_success=True,
        has_durable_state=True,
        container_hint="skill_candidate",
    )
    routed = await route_promotion_signal(signal, adjudicator=adjudicator)
    assert routed.status is RouteStatus.ROUTED
    assert routed.candidate.kind is PromotionKind.WORKFLOW_CANDIDATE
    assert routed.audit.get("adjudicated") is True


@pytest.mark.asyncio
async def test_missing_adjudicator_holds_ambiguous_signal() -> None:
    signal = _signal(category="reference", container_hint="soul_candidate")
    routed = await route_promotion_signal(signal, adjudicator=None)
    assert routed.status is RouteStatus.HELD
    assert routed.candidate is None
    assert routed.reason


@pytest.mark.asyncio
async def test_failing_adjudicator_holds_with_audit_reason() -> None:
    async def broken(signal: PromotionSignal, allowed: list[PromotionKind]) -> PromotionAdjudication:
        raise RuntimeError("LLM unavailable")

    signal = _signal(category="reference", container_hint="soul_candidate")
    routed = await route_promotion_signal(signal, adjudicator=broken)
    assert routed.status is RouteStatus.HELD
    assert "RuntimeError" in routed.audit.get("adjudicator_error", "")


@pytest.mark.asyncio
async def test_adjudicator_returning_disallowed_kind_is_held() -> None:
    async def rogue(signal: PromotionSignal, allowed: list[PromotionKind]) -> PromotionAdjudication:
        return PromotionAdjudication(kind=PromotionKind.SOUL_CANDIDATE, confidence=0.99, reason="trust me")

    # Escalation between skill and workflow must not come back as soul.
    signal = _signal(
        category="strategy",
        repeat_count=3,
        repeated_success=True,
        has_durable_state=True,
        container_hint="skill_candidate",
    )
    routed = await route_promotion_signal(signal, adjudicator=rogue)
    assert routed.status is RouteStatus.HELD


@pytest.mark.asyncio
async def test_unambiguous_signal_never_calls_adjudicator() -> None:
    calls = {"n": 0}

    async def counting(signal: PromotionSignal, allowed: list[PromotionKind]) -> PromotionAdjudication:
        calls["n"] += 1
        return PromotionAdjudication(kind=PromotionKind.MEMORY_APPEND, confidence=1.0, reason="x")

    routed = await route_promotion_signal(_signal(category="reference"), adjudicator=counting)
    assert routed.status is RouteStatus.ROUTED
    assert calls["n"] == 0
