"""External attention router for activation candidates."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.runtime.activation_candidates import ActivationCandidate, ActivationHardMask
from app.services.principal_context import PrincipalStack

ACTIVATION_ROUTER_OUTPUT_SCHEMA = "hive.ccplus.activation_router_output.v1"


def _text(value: Any, *, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def _string_tuple(value: Iterable[Any] | None) -> tuple[str, ...]:
    return tuple(text for item in value or () if (text := _text(item)))


def _first_feature(value: Any) -> str:
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, list | tuple | set | frozenset):
        for item in value:
            if text := _text(item):
                return text
    return _text(value)


def _candidate_field(candidate: ActivationCandidate, key: str, *, fallback: str = "") -> str:
    metadata_value = candidate.metadata.get(key)
    if text := _first_feature(metadata_value):
        return text
    return _first_feature(candidate.key_features.get(key)) or fallback


@dataclass(frozen=True, slots=True)
class ActivationRouterContext:
    principal_stack: PrincipalStack | None = None
    allowed_candidate_kinds: tuple[str, ...] = ()
    denied_candidate_kinds: tuple[str, ...] = ()
    query_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_candidate_kinds", _string_tuple(self.allowed_candidate_kinds))
        object.__setattr__(self, "denied_candidate_kinds", _string_tuple(self.denied_candidate_kinds))
        object.__setattr__(self, "query_id", _text(self.query_id))


@dataclass(frozen=True, slots=True)
class ActivationRouterOutput:
    query_id: str = ""
    top_activation_candidates: tuple[ActivationCandidate, ...] = ()
    suppressed_activation_candidates: tuple[ActivationCandidate, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema": ACTIVATION_ROUTER_OUTPUT_SCHEMA,
            "query_id": self.query_id,
            "top_activation_candidates": [candidate.to_manifest() for candidate in self.top_activation_candidates],
            "suppressed_activation_candidates": [
                candidate.to_manifest() for candidate in self.suppressed_activation_candidates
            ],
            "metadata": dict(self.metadata),
        }


def _with_hard_mask(candidate: ActivationCandidate, hard_mask: ActivationHardMask) -> ActivationCandidate:
    manifest = candidate.to_manifest()
    manifest["hard_mask"] = hard_mask.to_manifest()
    return ActivationCandidate.from_manifest(manifest)


def _policy_mask(candidate: ActivationCandidate, context: ActivationRouterContext) -> ActivationHardMask | None:
    kind = _text(candidate.candidate_kind)
    if context.allowed_candidate_kinds and kind not in context.allowed_candidate_kinds:
        return ActivationHardMask(
            allowed=False,
            reason="policy_denied",
            judge="activation_router",
            policy_ref="activation_router.allowed_candidate_kinds",
            details={"candidate_kind": kind},
        )
    if kind in context.denied_candidate_kinds:
        return ActivationHardMask(
            allowed=False,
            reason="policy_denied",
            judge="activation_router",
            policy_ref="activation_router.denied_candidate_kinds",
            details={"candidate_kind": kind},
        )
    return None


def _acl_mask(candidate: ActivationCandidate, principal_stack: PrincipalStack) -> ActivationHardMask | None:
    scope = _candidate_field(candidate, "acl_scope", fallback="company").lower()
    if scope in {"owner", "self", "private", "personal"} and not principal_stack.current_user_is_direct_owner:
        return ActivationHardMask(
            allowed=False,
            reason="acl_denied",
            judge="activation_router",
            policy_ref="activation_router.acl_scope",
            details={"acl_scope": scope},
        )
    if scope in {"company", "tenant"} and principal_stack.company is None:
        return ActivationHardMask(
            allowed=False,
            reason="acl_denied",
            judge="activation_router",
            policy_ref="activation_router.acl_scope",
            details={"acl_scope": scope},
        )
    return None


def _sensitivity_mask(candidate: ActivationCandidate, principal_stack: PrincipalStack) -> ActivationHardMask | None:
    sensitivity = _candidate_field(candidate, "sensitivity", fallback="PL1_public")
    if principal_stack.can_access_sensitivity(sensitivity):
        return None
    return ActivationHardMask(
        allowed=False,
        reason="sensitivity_denied",
        judge="activation_router",
        policy_ref="activation_router.sensitivity",
        details={"sensitivity": sensitivity},
    )


def route_activation_candidates(
    candidates: Iterable[ActivationCandidate],
    *,
    context: ActivationRouterContext,
) -> ActivationRouterOutput:
    principal_stack = context.principal_stack or PrincipalStack()
    top: list[ActivationCandidate] = []
    suppressed: list[ActivationCandidate] = []
    for candidate in candidates:
        hard_mask = (
            _policy_mask(candidate, context)
            or _acl_mask(candidate, principal_stack)
            or _sensitivity_mask(candidate, principal_stack)
        )
        if hard_mask is not None:
            suppressed.append(_with_hard_mask(candidate, hard_mask))
            continue
        top.append(candidate)
    return ActivationRouterOutput(
        query_id=context.query_id,
        top_activation_candidates=tuple(top),
        suppressed_activation_candidates=tuple(suppressed),
    )


__all__ = [
    "ACTIVATION_ROUTER_OUTPUT_SCHEMA",
    "ActivationRouterContext",
    "ActivationRouterOutput",
    "route_activation_candidates",
]
