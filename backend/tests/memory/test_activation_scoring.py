from __future__ import annotations

from datetime import UTC, datetime

from app.memory.activation import ActivationContext, ActivationPolicy, ActivationScorer, score_activation_factors
from app.memory.types import MemoryItem, MemoryKind
from app.services.principal_context import Principal, PrincipalRole, PrincipalStack


def _stack(current_user_id: str = "owner") -> PrincipalStack:
    return PrincipalStack(
        company=Principal(PrincipalRole.COMPANY, "tenant-1", "Acme"),
        direct_owner=Principal(PrincipalRole.OWNER, "owner", "Owner"),
        current_user=Principal(PrincipalRole.CURRENT_USER, current_user_id, "Current"),
    )


def test_activation_includes_reasons_for_goal_owner_and_open_loop() -> None:
    item = MemoryItem(
        kind=MemoryKind.SEMANTIC,
        content="Owner needs weekly Railway production log sweep follow-up.",
        score=0.5,
        source="test",
        metadata={"category": "project", "open_loop": True, "retention_score": 0.7},
    )
    context = ActivationContext(
        query="Railway log sweep",
        principal_stack=_stack(),
        goal_terms=["production", "railway"],
        owner_terms=["owner"],
    )

    decision = ActivationScorer().score(item, context)

    assert decision.score > item.score
    assert {"goal_relevance", "principal_relevance", "open_loop_pressure", "retention_score"} <= set(decision.reasons)


def test_activation_uses_unified_multiplicative_formula() -> None:
    policy = ActivationPolicy(context_boost_weight=0.3)
    item = MemoryItem(
        kind=MemoryKind.SEMANTIC,
        content="Railway deployment context.",
        score=0.5,
        source="test",
        metadata={"context_boost": "1.0"},
    )
    context = ActivationContext(query="deployment", principal_stack=_stack(), goal_terms=["railway"])

    decision = ActivationScorer(policy).score(item, context)
    expected = score_activation_factors(
        relevance_score=item.score,
        task_modulation_delta=policy.goal_weight,
        context_boost=1.0,
        policy=policy,
    )

    assert decision.raw_score == expected.raw_score
    assert decision.raw_score == 0.8125
    assert decision.raw_score < 1.0, "additive score += would saturate this case instead of multiplying"
    assert decision.score_trace["scorer"] == "unified_activation_multiplier.v1"
    assert decision.score_trace["formula"] == "relevance*context_boost*base_level*task_modulation"
    assert decision.score_trace["factors"]["context_boost"] == 1.3
    assert decision.score_trace["factors"]["task_modulation"] == 1.25


def test_activation_accepts_conf_alias_for_confidence_weight() -> None:
    item = MemoryItem(
        kind=MemoryKind.SEMANTIC,
        content="Owner prefers concise Railway incident summaries.",
        score=0.5,
        source="test",
        metadata={"conf": "0.91"},
    )
    context = ActivationContext(query="incident summaries", principal_stack=_stack(), owner_terms=["owner"])

    decision = ActivationScorer().score(item, context)

    assert "confidence_weight" in decision.reasons


def test_activation_uses_access_telemetry_as_bounded_base_level_signal() -> None:
    hot = MemoryItem(
        kind=MemoryKind.SEMANTIC,
        content="Legacy proxy timeout limit is still relevant.",
        score=0.35,
        source="test",
        metadata={"access_count": "12", "last_accessed": datetime.now(UTC).isoformat()},
    )
    cold = MemoryItem(
        kind=MemoryKind.SEMANTIC,
        content=hot.content,
        score=hot.score,
        source="test",
        metadata={"access_count": "0", "last_accessed": "never"},
    )
    context = ActivationContext(query="unrelated", principal_stack=_stack())

    hot_decision = ActivationScorer().score(hot, context)
    cold_decision = ActivationScorer().score(cold, context)

    assert hot_decision.score > cold_decision.score
    assert "base_level" in hot_decision.reasons
    assert "base_level" not in cold_decision.reasons


def test_activation_open_loop_false_string_does_not_score() -> None:
    item = MemoryItem(
        kind=MemoryKind.SEMANTIC,
        content="Owner prefers concise Railway incident summaries.",
        score=0.5,
        source="test",
        metadata={"open_loop": "false"},
    )
    context = ActivationContext(query="incident summaries", principal_stack=_stack(), owner_terms=["owner"])

    decision = ActivationScorer().score(item, context)

    assert "open_loop_pressure" not in decision.reasons


def test_activation_suppresses_pl3_when_current_user_is_not_owner() -> None:
    item = MemoryItem(
        kind=MemoryKind.SEMANTIC,
        content="Q3 salary plan is confidential.",
        score=0.9,
        source="test",
        metadata={"sensitivity": "PL3_sensitive"},
    )
    context = ActivationContext(query="salary", principal_stack=_stack(current_user_id="viewer"))

    decision = ActivationScorer().score(item, context)

    assert decision.suppressed
    assert decision.score == 0.0
    assert "sensitivity_strip" in decision.reasons
