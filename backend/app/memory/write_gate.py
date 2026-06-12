"""Shared safety gate for durable memory writes."""

from __future__ import annotations

import uuid
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Iterable

from app.memory.form_lint import enforce_memory_form
from app.services.privacy_layer import PrivacyLayer


@dataclass(frozen=True, slots=True)
class MemoryWriteDecision:
    original_content: str
    content: str
    category: str
    sensitivity: str
    metadata: dict[str, str] = field(default_factory=dict)
    rejected: bool = False
    reason: str = ""
    placeholders: dict[str, str] = field(default_factory=dict)


_MEMORY_THREAT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"ignore\s+(previous|all|above|prior)\s+instructions", re.IGNORECASE), "prompt_injection"),
    (re.compile(r"do\s+not\s+tell\s+the\s+user", re.IGNORECASE), "deception_hide"),
    (re.compile(r"system\s+prompt\s+override", re.IGNORECASE), "sys_prompt_override"),
    (re.compile(r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)", re.IGNORECASE), "disregard_rules"),
    (
        re.compile(
            r"act\s+as\s+(if|though)\s+you\s+(have\s+no|don't\s+have)\s+(restrictions|limits|rules)",
            re.IGNORECASE,
        ),
        "bypass_restrictions",
    ),
    (re.compile(r"reveal\s+(the\s+)?(hidden\s+)?system\s+prompt", re.IGNORECASE), "prompt_exfiltration"),
)


def prepare_memory_write(
    content: str,
    *,
    category: str,
    evidence_refs: Iterable[str] | str | None = None,
    status: str = "active",
    version: int | str = 1,
    parent_id: str | None = None,
    supersedes: Iterable[str] | str | None = None,
    superseded_by: str | None = None,
    expires_at: datetime | str | None = None,
    privacy_layer: PrivacyLayer | None = None,
    enforce_form: bool = True,
) -> MemoryWriteDecision:
    """Apply privacy, form, and lifecycle metadata before durable persistence."""
    original = (content or "").strip()
    normalized_category = (category or "general").strip().lower() or "general"
    threats = _detect_memory_threats(original)
    if threats:
        metadata = _base_metadata(
            sensitivity="PL3_prompt_injection",
            status="rejected",
            version=version,
            evidence_refs=evidence_refs,
            parent_id=parent_id,
            supersedes=supersedes,
            superseded_by=superseded_by,
            expires_at=expires_at,
        )
        reason = "prompt_injection:" + ",".join(threats)
        metadata["reason"] = _sanitize_meta_value(reason)
        return MemoryWriteDecision(
            original_content=original,
            content=original,
            category=normalized_category,
            sensitivity="PL3_prompt_injection",
            metadata=metadata,
            rejected=True,
            reason=reason,
        )

    layer = privacy_layer or PrivacyLayer()
    privacy_decision = layer.classify_and_mask(original)
    metadata = _base_metadata(
        sensitivity=privacy_decision.sensitivity.value,
        status=status,
        version=version,
        evidence_refs=evidence_refs,
        parent_id=parent_id,
        supersedes=supersedes,
        superseded_by=superseded_by,
        expires_at=expires_at,
    )

    if privacy_decision.rejected:
        metadata["status"] = "rejected"
        metadata["reason"] = _sanitize_meta_value(privacy_decision.reason)
        return MemoryWriteDecision(
            original_content=original,
            content=privacy_decision.sanitized_text,
            category=normalized_category,
            sensitivity=privacy_decision.sensitivity.value,
            metadata=metadata,
            rejected=True,
            reason=privacy_decision.reason,
            placeholders=privacy_decision.placeholders,
        )

    if enforce_form:
        try:
            enforce_memory_form(privacy_decision.sanitized_text)
        except ValueError as exc:
            metadata["status"] = "rejected"
            metadata["reason"] = _sanitize_meta_value(str(exc))
            return MemoryWriteDecision(
                original_content=original,
                content=privacy_decision.sanitized_text,
                category=normalized_category,
                sensitivity=privacy_decision.sensitivity.value,
                metadata=metadata,
                rejected=True,
                reason=str(exc),
                placeholders=privacy_decision.placeholders,
            )

    return MemoryWriteDecision(
        original_content=original,
        content=privacy_decision.sanitized_text,
        category=normalized_category,
        sensitivity=privacy_decision.sensitivity.value,
        metadata=metadata,
        placeholders=privacy_decision.placeholders,
    )


def _detect_memory_threats(text: str) -> list[str]:
    return [label for pattern, label in _MEMORY_THREAT_PATTERNS if pattern.search(text or "")]


def _base_metadata(
    *,
    sensitivity: str,
    status: str,
    version: int | str,
    evidence_refs: Iterable[str] | str | None,
    parent_id: str | None,
    supersedes: Iterable[str] | str | None,
    superseded_by: str | None,
    expires_at: datetime | str | None,
) -> dict[str, str]:
    metadata = {
        "entry_id": uuid.uuid4().hex,
        "sensitivity": _sanitize_meta_value(sensitivity),
        "status": _sanitize_meta_value(status),
        "version": _sanitize_meta_value(str(version)),
        # access_count/last_accessed seed the sidecar record's initial telemetry.
        # D1 keeps them OUT of T3 `.md` prose via append_t3_entry's filter; the
        # sidecar's own integer fields (bumped on recall) are the live source of
        # truth. (write_gate is shared with the T2 path, so the seed stays here.)
        "access_count": "0",
        "last_accessed": "never",
    }
    refs = _join_refs(evidence_refs)
    if refs:
        metadata["evidence_refs"] = refs
    if parent_id:
        metadata["parent_id"] = _sanitize_meta_value(parent_id)
    supersedes_value = _join_refs(supersedes)
    if supersedes_value:
        metadata["supersedes"] = supersedes_value
    if superseded_by:
        metadata["superseded_by"] = _sanitize_meta_value(superseded_by)
    if expires_at:
        metadata["expires_at"] = _format_time(expires_at)
    return metadata


def _join_refs(value: Iterable[str] | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        raw = value.split(",")
    else:
        raw = list(value)
    return ",".join(_sanitize_meta_value(str(ref).strip()) for ref in raw if str(ref).strip())


def _format_time(value: datetime | str) -> str:
    if isinstance(value, datetime):
        timestamp = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
        return timestamp.isoformat()
    return _sanitize_meta_value(value)


def _sanitize_meta_value(value: str) -> str:
    return " ".join(str(value).replace("[", "(").replace("]", ")").split())
