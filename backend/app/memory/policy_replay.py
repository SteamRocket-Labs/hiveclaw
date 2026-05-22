"""Replay guard for activation-policy experiments."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.memory.activation import ActivationContext, ActivationPolicy, ActivationScorer
from app.memory.types import MemoryItem


@dataclass(frozen=True, slots=True)
class ReplayCase:
    case_id: str
    context: ActivationContext
    candidates: list[MemoryItem]
    expected_entry_ids: set[str]


@dataclass(frozen=True, slots=True)
class ReplayReport:
    hit_rate: float
    total_cases: int
    hit_cases: int
    misses: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PolicyExperimentResult:
    accepted: bool
    reverted: bool
    baseline_policy: ActivationPolicy
    candidate_policy: ActivationPolicy
    rollback_policy: ActivationPolicy | None
    baseline: ReplayReport
    candidate: ReplayReport
    reasons: list[str] = field(default_factory=list)


def evaluate_activation_policy(
    *,
    policy: ActivationPolicy,
    cases: list[ReplayCase],
    top_k: int = 3,
) -> ReplayReport:
    scorer = ActivationScorer(policy=policy)
    hits = 0
    misses: list[str] = []
    for case in cases:
        scored = [
            decision
            for decision in (scorer.score(item, case.context) for item in case.candidates)
            if not decision.suppressed
        ]
        scored.sort(key=lambda decision: decision.score, reverse=True)
        top_entry_ids = {str(decision.item.metadata.get("entry_id", "")) for decision in scored[: max(top_k, 1)]}
        if case.expected_entry_ids & top_entry_ids:
            hits += 1
        else:
            misses.append(case.case_id)

    total = len(cases)
    return ReplayReport(
        hit_rate=round(hits / total, 4) if total else 0.0,
        total_cases=total,
        hit_cases=hits,
        misses=misses,
    )


def guard_activation_policy_experiment(
    *,
    baseline_policy: ActivationPolicy,
    candidate_policy: ActivationPolicy,
    cases: list[ReplayCase],
    top_k: int = 3,
    max_allowed_drop: float = 0.0,
) -> PolicyExperimentResult:
    baseline = evaluate_activation_policy(policy=baseline_policy, cases=cases, top_k=top_k)
    candidate = evaluate_activation_policy(policy=candidate_policy, cases=cases, top_k=top_k)

    reasons: list[str] = []
    quality_drop = baseline.hit_rate - candidate.hit_rate
    if quality_drop > max_allowed_drop:
        reasons.append("candidate_quality_drop")
        return PolicyExperimentResult(
            accepted=False,
            reverted=True,
            baseline_policy=baseline_policy,
            candidate_policy=candidate_policy,
            rollback_policy=baseline_policy,
            baseline=baseline,
            candidate=candidate,
            reasons=reasons,
        )

    if candidate.hit_rate > baseline.hit_rate:
        reasons.append("candidate_improved_replay")
    else:
        reasons.append("candidate_within_guardrail")

    return PolicyExperimentResult(
        accepted=True,
        reverted=False,
        baseline_policy=baseline_policy,
        candidate_policy=candidate_policy,
        rollback_policy=None,
        baseline=baseline,
        candidate=candidate,
        reasons=reasons,
    )
