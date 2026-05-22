from __future__ import annotations

from app.memory.activation import ActivationContext, ActivationPolicy
from app.memory.types import MemoryItem, MemoryKind
from app.services.principal_context import Principal, PrincipalRole, PrincipalStack


def _context() -> ActivationContext:
    return ActivationContext(
        query="investor memo follow-up",
        principal_stack=PrincipalStack(
            company=Principal(PrincipalRole.COMPANY, "tenant-1", "Acme"),
            direct_owner=Principal(PrincipalRole.OWNER, "owner-1", "Alice"),
            current_user=Principal(PrincipalRole.CURRENT_USER, "owner-1", "Alice"),
        ),
        goal_terms=["investor", "memo"],
        owner_terms=["alice"],
        company_terms=["acme"],
    )


def _item(entry_id: str, content: str, score: float = 0.4) -> MemoryItem:
    return MemoryItem(
        kind=MemoryKind.SEMANTIC,
        content=content,
        score=score,
        source="test",
        metadata={"entry_id": entry_id},
    )


def test_activation_policy_replay_accepts_candidate_that_improves_hits() -> None:
    from app.memory.policy_replay import ReplayCase, guard_activation_policy_experiment

    cases = [
        ReplayCase(
            case_id="case-1",
            context=_context(),
            candidates=[
                _item("expected", "Alice needs investor memo follow-up prepared.", score=0.3),
                _item("distractor", "Random low-priority lunch preference.", score=0.6),
            ],
            expected_entry_ids={"expected"},
        )
    ]

    result = guard_activation_policy_experiment(
        baseline_policy=ActivationPolicy(goal_weight=0.05, owner_weight=0.0, company_weight=0.0),
        candidate_policy=ActivationPolicy(goal_weight=0.45, owner_weight=0.2, company_weight=0.1),
        cases=cases,
        top_k=1,
    )

    assert result.accepted is True
    assert result.reverted is False
    assert result.candidate.hit_rate > result.baseline.hit_rate


def test_activation_policy_replay_reverts_candidate_when_quality_drops() -> None:
    from app.memory.policy_replay import ReplayCase, guard_activation_policy_experiment

    cases = [
        ReplayCase(
            case_id="case-1",
            context=_context(),
            candidates=[
                _item("expected", "Alice needs investor memo follow-up prepared.", score=0.5),
                _item("distractor", "Random item with no owner or goal relevance.", score=0.55),
            ],
            expected_entry_ids={"expected"},
        )
    ]

    result = guard_activation_policy_experiment(
        baseline_policy=ActivationPolicy(goal_weight=0.25, owner_weight=0.2),
        candidate_policy=ActivationPolicy(goal_weight=0.0, owner_weight=0.0, company_weight=0.0),
        cases=cases,
        top_k=1,
    )

    assert result.accepted is False
    assert result.reverted is True
    assert result.rollback_policy == result.baseline_policy
    assert "candidate_quality_drop" in result.reasons
