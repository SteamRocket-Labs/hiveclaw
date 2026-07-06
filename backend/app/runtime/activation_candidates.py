"""Activation candidate contracts for the external attention router."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

ACTIVATION_CANDIDATE_SCHEMA = "hive.ccplus.activation_candidate.v1"
ACTIVATION_SCORE_SCHEMA = "hive.ccplus.activation_score.v1"
ACTIVATION_HARD_MASK_SCHEMA = "hive.ccplus.activation_hard_mask.v1"
ACTIVATION_SURFACE_SCHEMA = "hive.ccplus.activation_surface.v1"


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


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, list | tuple | set | frozenset):
        items = list(value)
    else:
        items = []
    return tuple(text for item in items if (text := _text(item)))


def _float_dict(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, float] = {}
    for key, child in value.items():
        try:
            result[_text(key)] = float(child)
        except (TypeError, ValueError):
            continue
    return {key: score for key, score in result.items() if key}


def activation_candidate_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ActivationScore:
    head_scores: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    total_score: float | None = None
    scorer: str = "runtime_activation"

    def __post_init__(self) -> None:
        head_scores = _float_dict(self.head_scores)
        weights = _float_dict(self.weights)
        reasons = _string_tuple(self.reasons)
        object.__setattr__(self, "head_scores", head_scores)
        object.__setattr__(self, "weights", weights)
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "scorer", _text(self.scorer, fallback="runtime_activation"))
        if self.total_score is None:
            object.__setattr__(self, "total_score", _weighted_total(head_scores, weights))
        else:
            object.__setattr__(self, "total_score", float(self.total_score))

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema": ACTIVATION_SCORE_SCHEMA,
            "head_scores": dict(self.head_scores),
            "weights": dict(self.weights),
            "total_score": self.total_score,
            "reasons": list(self.reasons),
            "scorer": self.scorer,
        }

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any]) -> "ActivationScore":
        schema = manifest.get("schema")
        if schema and schema != ACTIVATION_SCORE_SCHEMA:
            raise ValueError(f"Unsupported activation score schema: {schema!r}")
        return cls(
            head_scores=_float_dict(manifest.get("head_scores")),
            weights=_float_dict(manifest.get("weights")),
            total_score=manifest.get("total_score"),
            reasons=_string_tuple(manifest.get("reasons")),
            scorer=_text(manifest.get("scorer"), fallback="runtime_activation"),
        )


def _weighted_total(head_scores: dict[str, float], weights: dict[str, float]) -> float:
    if not head_scores:
        return 0.0
    numerator = 0.0
    denominator = 0.0
    for key, score in head_scores.items():
        weight = weights.get(key, 1.0)
        numerator += score * weight
        denominator += abs(weight)
    return numerator / denominator if denominator else 0.0


@dataclass(frozen=True, slots=True)
class ActivationHardMask:
    allowed: bool = True
    reason: str = "allowed"
    judge: str = "platform_gate"
    policy_ref: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed", bool(self.allowed))
        object.__setattr__(self, "reason", _text(self.reason, fallback="allowed"))
        object.__setattr__(self, "judge", _text(self.judge, fallback="platform_gate"))
        object.__setattr__(self, "policy_ref", _text(self.policy_ref))
        object.__setattr__(self, "details", _dict_payload(self.details))

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema": ACTIVATION_HARD_MASK_SCHEMA,
            "allowed": self.allowed,
            "reason": self.reason,
            "judge": self.judge,
            "policy_ref": self.policy_ref,
            "details": dict(self.details),
        }

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any]) -> "ActivationHardMask":
        schema = manifest.get("schema")
        if schema and schema != ACTIVATION_HARD_MASK_SCHEMA:
            raise ValueError(f"Unsupported activation hard mask schema: {schema!r}")
        return cls(
            allowed=bool(manifest.get("allowed", True)),
            reason=_text(manifest.get("reason"), fallback="allowed"),
            judge=_text(manifest.get("judge"), fallback="platform_gate"),
            policy_ref=_text(manifest.get("policy_ref")),
            details=_dict_payload(manifest.get("details")),
        )


@dataclass(frozen=True, slots=True)
class ActivationSurface:
    surface_kind: str = "hint"
    preview: str = ""
    token_estimate: int = 0
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "surface_kind", _text(self.surface_kind, fallback="hint"))
        object.__setattr__(self, "preview", _text(self.preview))
        object.__setattr__(self, "token_estimate", max(0, int(self.token_estimate or 0)))
        object.__setattr__(self, "source_refs", _string_tuple(self.source_refs))

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema": ACTIVATION_SURFACE_SCHEMA,
            "surface_kind": self.surface_kind,
            "preview": self.preview,
            "token_estimate": self.token_estimate,
            "source_refs": list(self.source_refs),
        }

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any]) -> "ActivationSurface":
        schema = manifest.get("schema")
        if schema and schema != ACTIVATION_SURFACE_SCHEMA:
            raise ValueError(f"Unsupported activation surface schema: {schema!r}")
        return cls(
            surface_kind=_text(manifest.get("surface_kind"), fallback="hint"),
            preview=_text(manifest.get("preview")),
            token_estimate=int(manifest.get("token_estimate") or 0),
            source_refs=_string_tuple(manifest.get("source_refs")),
        )


@dataclass(frozen=True, slots=True)
class ActivationCandidate:
    candidate_kind: str
    candidate_ref: dict[str, Any]
    key_features: dict[str, Any] = field(default_factory=dict)
    value_pointer: dict[str, Any] = field(default_factory=dict)
    surface: ActivationSurface | Mapping[str, Any] = field(default_factory=ActivationSurface)
    source_refs: tuple[str, ...] = ()
    score: ActivationScore | Mapping[str, Any] | None = None
    hard_mask: ActivationHardMask | Mapping[str, Any] = field(default_factory=ActivationHardMask)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        surface = self.surface if isinstance(self.surface, ActivationSurface) else ActivationSurface.from_manifest(self.surface)
        score = self.score
        if score is not None and not isinstance(score, ActivationScore):
            score = ActivationScore.from_manifest(score)
        hard_mask = (
            self.hard_mask
            if isinstance(self.hard_mask, ActivationHardMask)
            else ActivationHardMask.from_manifest(self.hard_mask)
        )
        source_refs = _string_tuple(self.source_refs) or surface.source_refs
        object.__setattr__(self, "candidate_kind", _text(self.candidate_kind, fallback="unknown"))
        object.__setattr__(self, "candidate_ref", _dict_payload(self.candidate_ref))
        object.__setattr__(self, "key_features", _dict_payload(self.key_features))
        object.__setattr__(self, "value_pointer", _dict_payload(self.value_pointer))
        object.__setattr__(self, "surface", surface)
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "hard_mask", hard_mask)
        object.__setattr__(self, "metadata", _dict_payload(self.metadata))

    @property
    def candidate_id(self) -> str:
        ref_id = _text(self.candidate_ref.get("candidate_id") or self.candidate_ref.get("id"))
        if ref_id:
            return ref_id
        payload = {
            "candidate_kind": self.candidate_kind,
            "candidate_ref": self.candidate_ref,
            "key_features": self.key_features,
            "value_pointer": self.value_pointer,
        }
        return f"{self.candidate_kind}:{activation_candidate_hash(payload)[:16]}"

    @property
    def is_allowed(self) -> bool:
        return self.hard_mask.allowed

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema": ACTIVATION_CANDIDATE_SCHEMA,
            "candidate_id": self.candidate_id,
            "candidate_kind": self.candidate_kind,
            "candidate_ref": dict(self.candidate_ref),
            "key_features": dict(self.key_features),
            "value_pointer": dict(self.value_pointer),
            "surface": self.surface.to_manifest(),
            "source_refs": list(self.source_refs),
            "score": self.score.to_manifest() if self.score else None,
            "hard_mask": self.hard_mask.to_manifest(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any]) -> "ActivationCandidate":
        if manifest.get("schema") != ACTIVATION_CANDIDATE_SCHEMA:
            raise ValueError(f"Unsupported activation candidate schema: {manifest.get('schema')!r}")
        return cls(
            candidate_kind=_text(manifest.get("candidate_kind"), fallback="unknown"),
            candidate_ref=_dict_payload(manifest.get("candidate_ref")),
            key_features=_dict_payload(manifest.get("key_features")),
            value_pointer=_dict_payload(manifest.get("value_pointer")),
            surface=ActivationSurface.from_manifest(_dict_payload(manifest.get("surface"))),
            source_refs=_string_tuple(manifest.get("source_refs")),
            score=ActivationScore.from_manifest(_dict_payload(manifest.get("score"))) if manifest.get("score") else None,
            hard_mask=ActivationHardMask.from_manifest(_dict_payload(manifest.get("hard_mask"))),
            metadata=_dict_payload(manifest.get("metadata")),
        )


__all__ = [
    "ACTIVATION_CANDIDATE_SCHEMA",
    "ACTIVATION_HARD_MASK_SCHEMA",
    "ACTIVATION_SCORE_SCHEMA",
    "ACTIVATION_SURFACE_SCHEMA",
    "ActivationCandidate",
    "ActivationHardMask",
    "ActivationScore",
    "ActivationSurface",
    "activation_candidate_hash",
]
