"""Decision trace and feedback-link store."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class DecisionTrace:
    id: str
    action: str
    chosen: str
    reasoning: str
    alternatives_considered: list[str]
    situational_factors: list[str]
    charter_zone: str
    preflight: dict[str, str]
    sensitivity: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class FeedbackSignal:
    id: str
    refs: str
    reaction: str
    polarity: str
    source: str
    rationale_from_owner: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class DecisionTraceStore:
    def __init__(self) -> None:
        self._decisions: dict[str, DecisionTrace] = {}
        self._feedback: list[FeedbackSignal] = []

    def record_decision(
        self,
        *,
        action: str,
        chosen: str,
        reasoning: str,
        alternatives_considered: list[str],
        situational_factors: list[str],
        charter_zone: str,
        preflight: dict[str, str],
        sensitivity: str,
    ) -> DecisionTrace:
        decision = DecisionTrace(
            id=str(uuid.uuid4()),
            action=action,
            chosen=chosen,
            reasoning=reasoning,
            alternatives_considered=alternatives_considered,
            situational_factors=situational_factors,
            charter_zone=charter_zone,
            preflight=preflight,
            sensitivity=sensitivity,
        )
        self._decisions[decision.id] = decision
        return decision

    def record_feedback(
        self,
        *,
        decision_id: str,
        reaction: str,
        polarity: str,
        source: str,
        rationale_from_owner: str = "",
    ) -> FeedbackSignal:
        if decision_id not in self._decisions:
            raise KeyError(decision_id)
        feedback = FeedbackSignal(
            id=str(uuid.uuid4()),
            refs=f"decision/{decision_id}",
            reaction=reaction,
            polarity=polarity,
            source=source,
            rationale_from_owner=rationale_from_owner,
        )
        self._feedback.append(feedback)
        return feedback

    def feedback_for_decision(self, decision_id: str) -> list[FeedbackSignal]:
        ref = f"decision/{decision_id}"
        return [feedback for feedback in self._feedback if feedback.refs == ref]

    def decisions(self) -> list[DecisionTrace]:
        return list(self._decisions.values())

    def calibration_candidates(self) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        for feedback in self._feedback:
            if feedback.reaction == "unclear":
                continue
            decision_id = feedback.refs.removeprefix("decision/")
            decision = self._decisions.get(decision_id)
            if not decision:
                continue
            candidates.append(
                {
                    "decision_id": decision.id,
                    "action": decision.action,
                    "reaction": feedback.reaction,
                    "charter_zone": decision.charter_zone,
                }
            )
        return candidates
