"""Decision trace and feedback-link store."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


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
    def __init__(self, path: Path | None = None) -> None:
        self._decisions: dict[str, DecisionTrace] = {}
        self._feedback: list[FeedbackSignal] = []
        self._path = path
        self._load()

    @classmethod
    def persistent_default(cls) -> DecisionTraceStore:
        return cls(path=cls._default_path())

    @staticmethod
    def _default_path() -> Path | None:
        try:
            from app.config import get_settings

            return Path(get_settings().AGENT_DATA_DIR) / "_control_plane" / "decision_traces.jsonl"
        except Exception:  # noqa: BLE001
            return None

    def _append(self, payload: dict[str, Any]) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("event") == "decision":
                data = payload.get("decision") or {}
                try:
                    decision = DecisionTrace(
                        id=str(data["id"]),
                        action=str(data["action"]),
                        chosen=str(data["chosen"]),
                        reasoning=str(data["reasoning"]),
                        alternatives_considered=[str(item) for item in data.get("alternatives_considered") or []],
                        situational_factors=[str(item) for item in data.get("situational_factors") or []],
                        charter_zone=str(data["charter_zone"]),
                        preflight={str(k): str(v) for k, v in (data.get("preflight") or {}).items()},
                        sensitivity=str(data["sensitivity"]),
                        created_at=datetime.fromisoformat(str(data["created_at"])),
                    )
                except (KeyError, ValueError, TypeError):
                    continue
                self._decisions[decision.id] = decision
            elif payload.get("event") == "feedback":
                data = payload.get("feedback") or {}
                try:
                    feedback = FeedbackSignal(
                        id=str(data["id"]),
                        refs=str(data["refs"]),
                        reaction=str(data["reaction"]),
                        polarity=str(data["polarity"]),
                        source=str(data["source"]),
                        rationale_from_owner=str(data.get("rationale_from_owner") or ""),
                        created_at=datetime.fromisoformat(str(data["created_at"])),
                    )
                except (KeyError, ValueError, TypeError):
                    continue
                self._feedback.append(feedback)

    def _decision_to_dict(self, decision: DecisionTrace) -> dict[str, Any]:
        return {
            "id": decision.id,
            "action": decision.action,
            "chosen": decision.chosen,
            "reasoning": decision.reasoning,
            "alternatives_considered": decision.alternatives_considered,
            "situational_factors": decision.situational_factors,
            "charter_zone": decision.charter_zone,
            "preflight": decision.preflight,
            "sensitivity": decision.sensitivity,
            "created_at": decision.created_at.isoformat(),
        }

    def _feedback_to_dict(self, feedback: FeedbackSignal) -> dict[str, Any]:
        return {
            "id": feedback.id,
            "refs": feedback.refs,
            "reaction": feedback.reaction,
            "polarity": feedback.polarity,
            "source": feedback.source,
            "rationale_from_owner": feedback.rationale_from_owner,
            "created_at": feedback.created_at.isoformat(),
        }

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
        self._append({"schema": "decision_trace_event.v1", "event": "decision", "decision": self._decision_to_dict(decision)})
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
        self._append({"schema": "decision_trace_event.v1", "event": "feedback", "feedback": self._feedback_to_dict(feedback)})
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
