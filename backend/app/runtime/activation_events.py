"""Activation feedback event contracts.

Activation events are control-plane credit-assignment records. They may affect
future heat, decay, and scoring, but they do not mutate T0/T2/T3 truth surfaces.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

ACTIVATION_EVENT_SCHEMA = "hive.ccplus.activation_event.v1"
ACTIVATION_FEEDBACK_SCHEMA = "hive.ccplus.activation_feedback.v1"
ACTIVATION_EVENT_REF_SCHEMA = "hive.ccplus.activation_event_ref.v1"
ACTIVATION_EVENT_TRUTH_SURFACE_POLICY_SCHEMA = "hive.ccplus.activation_event_truth_surface_policy.v1"


def _text(value: Any, *, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_safe(child) for child in value]
    return str(value)


def _dict_payload(value: Any) -> dict[str, Any]:
    return _json_safe(value) if isinstance(value, Mapping) else {}


def activation_event_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ActivationFeedback:
    signal: str
    outcome: str = "unknown"
    credit: float = 0.0
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal", _text(self.signal, fallback="unknown"))
        object.__setattr__(self, "outcome", _text(self.outcome, fallback="unknown"))
        object.__setattr__(self, "credit", float(self.credit or 0.0))
        object.__setattr__(self, "reason", _text(self.reason))
        object.__setattr__(self, "details", _dict_payload(self.details))

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema": ACTIVATION_FEEDBACK_SCHEMA,
            "signal": self.signal,
            "outcome": self.outcome,
            "credit": self.credit,
            "reason": self.reason,
            "details": dict(self.details),
        }

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any]) -> "ActivationFeedback":
        schema = manifest.get("schema")
        if schema and schema != ACTIVATION_FEEDBACK_SCHEMA:
            raise ValueError(f"Unsupported activation feedback schema: {schema!r}")
        return cls(
            signal=_text(manifest.get("signal"), fallback="unknown"),
            outcome=_text(manifest.get("outcome"), fallback="unknown"),
            credit=float(manifest.get("credit") or 0.0),
            reason=_text(manifest.get("reason")),
            details=_dict_payload(manifest.get("details")),
        )


@dataclass(frozen=True, slots=True)
class ActivationEvent:
    event_type: str
    session_id: str
    turn_id: str
    intent_id: str
    query_id: str = ""
    candidate_id: str = ""
    candidate_ref: dict[str, Any] = field(default_factory=dict)
    feedback: ActivationFeedback | Mapping[str, Any] | None = None
    created_at: str = ""
    source: str = "runtime"
    metadata: dict[str, Any] = field(default_factory=dict)
    event_id: str = ""

    def __post_init__(self) -> None:
        feedback = self.feedback
        if feedback is not None and not isinstance(feedback, ActivationFeedback):
            feedback = ActivationFeedback.from_manifest(feedback)
        normalized = {
            "event_type": _text(self.event_type, fallback="unknown"),
            "session_id": _text(self.session_id),
            "turn_id": _text(self.turn_id),
            "intent_id": _text(self.intent_id),
            "query_id": _text(self.query_id),
            "candidate_id": _text(self.candidate_id),
            "candidate_ref": _dict_payload(self.candidate_ref),
            "feedback": feedback.to_manifest() if feedback else None,
            "created_at": _text(self.created_at),
            "source": _text(self.source, fallback="runtime"),
            "metadata": _dict_payload(self.metadata),
        }
        for key, value in normalized.items():
            if key == "feedback":
                continue
            object.__setattr__(self, key, value)
        object.__setattr__(self, "feedback", feedback)
        if not _text(self.event_id):
            object.__setattr__(self, "event_id", f"ae:{activation_event_hash(normalized)[:24]}")
        else:
            object.__setattr__(self, "event_id", _text(self.event_id))

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema": ACTIVATION_EVENT_SCHEMA,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "intent_id": self.intent_id,
            "query_id": self.query_id,
            "candidate_id": self.candidate_id,
            "candidate_ref": dict(self.candidate_ref),
            "feedback": self.feedback.to_manifest() if self.feedback else None,
            "created_at": self.created_at,
            "source": self.source,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any]) -> "ActivationEvent":
        if manifest.get("schema") != ACTIVATION_EVENT_SCHEMA:
            raise ValueError(f"Unsupported activation event schema: {manifest.get('schema')!r}")
        feedback = manifest.get("feedback")
        return cls(
            event_id=_text(manifest.get("event_id")),
            event_type=_text(manifest.get("event_type"), fallback="unknown"),
            session_id=_text(manifest.get("session_id")),
            turn_id=_text(manifest.get("turn_id")),
            intent_id=_text(manifest.get("intent_id")),
            query_id=_text(manifest.get("query_id")),
            candidate_id=_text(manifest.get("candidate_id")),
            candidate_ref=_dict_payload(manifest.get("candidate_ref")),
            feedback=ActivationFeedback.from_manifest(feedback) if isinstance(feedback, Mapping) else None,
            created_at=_text(manifest.get("created_at")),
            source=_text(manifest.get("source"), fallback="runtime"),
            metadata=_dict_payload(manifest.get("metadata")),
        )


def build_activation_event_ref(event: ActivationEvent | Mapping[str, Any]) -> dict[str, Any]:
    activation_event = event if isinstance(event, ActivationEvent) else ActivationEvent.from_manifest(event)
    manifest = activation_event.to_manifest()
    return {
        "schema": ACTIVATION_EVENT_REF_SCHEMA,
        "kind": "activation_event",
        "event_id": activation_event.event_id,
        "event_type": activation_event.event_type,
        "session_id": activation_event.session_id,
        "turn_id": activation_event.turn_id,
        "intent_id": activation_event.intent_id,
        "query_id": activation_event.query_id,
        "candidate_id": activation_event.candidate_id,
        "content_hash": activation_event_hash(manifest),
    }


def activation_event_truth_surface_policy() -> dict[str, Any]:
    """Declare that activation telemetry is a control sidecar, not memory truth."""
    return {
        "schema": ACTIVATION_EVENT_TRUTH_SURFACE_POLICY_SCHEMA,
        "durable_truth_mutation": False,
        "allowed_sidecar_roots": ["memory/control/"],
        "forbidden_truth_surfaces": [
            "memory/t0/",
            "memory/t2/",
            "memory/t3/",
            "memory/knowledge/",
            "memory/profiles/",
            "soul.md",
        ],
    }


__all__ = [
    "ACTIVATION_EVENT_REF_SCHEMA",
    "ACTIVATION_EVENT_SCHEMA",
    "ACTIVATION_FEEDBACK_SCHEMA",
    "ACTIVATION_EVENT_TRUTH_SURFACE_POLICY_SCHEMA",
    "ActivationEvent",
    "ActivationFeedback",
    "activation_event_truth_surface_policy",
    "activation_event_hash",
    "build_activation_event_ref",
]
