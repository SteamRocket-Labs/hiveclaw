from __future__ import annotations

from app.memory.activation import ActivationContext, ActivationScorer
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

