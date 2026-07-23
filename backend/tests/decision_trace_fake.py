from __future__ import annotations

import uuid
from typing import Any

from app.services.decision_trace import (
    DecisionTrace,
    FeedbackSignal,
    decision_id_from_ref,
    normalize_decision_ref,
)


class InMemoryDecisionTraceStore:
    """Unit-test double; production persistence is SQL-only."""

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
        tenant_id: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        message_id: str | None = None,
        tool_name: str | None = None,
        checkpoint_id: str | None = None,
        **_unused: Any,
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
            tenant_id=str(tenant_id) if tenant_id else None,
            agent_id=str(agent_id) if agent_id else None,
            user_id=str(user_id) if user_id else None,
            session_id=str(session_id) if session_id else None,
            message_id=str(message_id) if message_id else None,
            tool_name=str(tool_name) if tool_name else None,
            checkpoint_id=str(checkpoint_id) if checkpoint_id else None,
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
        normalized_id = decision_id_from_ref(decision_id)
        if normalized_id not in self._decisions:
            raise KeyError(normalized_id)
        feedback = FeedbackSignal(
            id=str(uuid.uuid4()),
            refs=normalize_decision_ref(normalized_id),
            reaction=reaction,
            polarity=polarity,
            source=source,
            rationale_from_owner=rationale_from_owner,
        )
        self._feedback.append(feedback)
        return feedback

    def feedback_for_decision(self, decision_id: str) -> list[FeedbackSignal]:
        ref = normalize_decision_ref(decision_id)
        return [feedback for feedback in self._feedback if feedback.refs == ref]

    def get_decision(self, decision_id: str) -> DecisionTrace:
        return self._decisions[decision_id_from_ref(decision_id)]

    def decisions(self) -> list[DecisionTrace]:
        return list(self._decisions.values())
