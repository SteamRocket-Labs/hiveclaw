"""Deterministic helpers for T2 engineering labels.

Event and fact labels remain LLM-authored. Engineering labels need bounded,
auditable rules so numeric fields cannot degrade into model vibes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable


_SOURCE_INTEGRITY_SCORES = {
    "complete": 1.00,
    "partial": 0.70,
    "replayed": 0.60,
    "missing_refs": 0.25,
}
_CLOSURE_SCORES = {
    "closed": 1.00,
    "rolling_checkpoint": 0.75,
    "open": 0.55,
    "archived_recall_only": 0.60,
    "rejected": 0.25,
}
_PENALTY_VALUES = {
    "unresolved_contested_point": 0.15,
    "correction_not_reflected": 0.20,
    "principal_scope_unknown_visibility": 0.10,
    "sensitive_level_uncertain": 0.20,
    "prompt_injection_not_isolated": 0.20,
}
_SYSTEM_MAX = 5


def source_integrity_score(value: str | None) -> float:
    return _SOURCE_INTEGRITY_SCORES.get((value or "").strip().lower(), _SOURCE_INTEGRITY_SCORES["missing_refs"])


def closure_score(value: str | None) -> float:
    return _CLOSURE_SCORES.get((value or "").strip().lower(), _CLOSURE_SCORES["open"])


def compute_engineering_confidence(
    *,
    evidence_coverage: float,
    source_integrity: str,
    label_specificity: float,
    internal_consistency: float,
    closure_status: str,
    penalties: Iterable[str] | None = None,
) -> float:
    raw = (
        0.40 * _clamp(evidence_coverage)
        + 0.20 * source_integrity_score(source_integrity)
        + 0.15 * _clamp(label_specificity)
        + 0.15 * _clamp(internal_consistency)
        + 0.10 * closure_score(closure_status)
        - sum(_PENALTY_VALUES.get(str(penalty).strip(), 0.0) for penalty in (penalties or []))
    )
    return _round_to_005(_clamp(raw))


def derive_risk_flags(
    *,
    text: str,
    source_integrity: str,
    principal_scope: str | None = None,
    sensitivity: str | None = None,
) -> list[str]:
    lowered = (text or "").lower()
    flags: list[str] = []
    if _contains_privacy_marker(text):
        flags.append("privacy_sensitive")
    if "tenant" in lowered or "cross-tenant" in lowered or (principal_scope or "").strip().lower() == "unknown":
        flags.append("cross_tenant")
    if re.search(r"\b(auth|token|rls|permission|secret|sandbox|mcp|credential|越权|权限|密钥)\b", lowered):
        flags.append("security_relevant")
    if re.search(r"\b(railway|deploy|production|migration|outage|prod|runtime)\b", lowered):
        flags.append("production_impact")
    if "policy conflict" in lowered or "charter conflict" in lowered or "规则冲突" in text:
        flags.append("policy_conflict")
    if source_integrity_score(source_integrity) < 1.0 or (sensitivity or "").upper().startswith(("PL3", "PL4")):
        flags.append("evidence_gap")
    return _dedupe(flags)


def normalize_systems(values: Iterable[str], *, registry: set[str] | frozenset[str]) -> list[str]:
    allowed = {item.strip().lower() for item in registry if item.strip()}
    normalized: list[str] = []
    for raw in values:
        value = str(raw).strip().lower()
        if value and value in allowed and value not in normalized:
            normalized.append(value)
        if len(normalized) >= _SYSTEM_MAX:
            break
    return normalized


def _contains_privacy_marker(text: str) -> bool:
    return bool(
        re.search(r"\b(email|phone|address|passport|ssn|private|personal)\b", text or "", re.IGNORECASE)
        or re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text or "")
    )


def _clamp(value: float | int | str | None) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.0
    return max(0.0, min(parsed, 1.0))


def _round_to_005(value: float) -> float:
    return round(round(value / 0.05) * 0.05, 2)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result

