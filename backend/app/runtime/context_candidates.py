"""Unified context-candidate reference identifiers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Mapping


_REF_PART_RE = re.compile(r"[^a-z0-9_.-]+")
_KIND_ALIASES = {
    "skills": "skill",
    "skill_catalog": "skill",
    "system_tools": "tool_schema",
}


def _normalize_ref_part(value: Any, *, fallback: str) -> str:
    text = str(value or "").strip().lower()
    normalized = _REF_PART_RE.sub("_", text).strip("_")
    return normalized or fallback


def _payload_hash(payload: Any) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        text = str(payload or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class ContextCandidateRef:
    kind: str
    item_id: str
    version: str
    content_hash: str

    @property
    def candidate_id(self) -> str:
        suffix = self.version
        if self.content_hash:
            suffix = f"{suffix}/{self.content_hash}" if suffix else self.content_hash
        return f"{self.kind}:{self.item_id}:{suffix}"

    def to_manifest(self, *, legacy_id: str | None = None) -> dict[str, Any]:
        payload = {
            "schema": "hive.ccplus.context_candidate_ref.v1",
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "item_id": self.item_id,
            "version": self.version,
            "content_hash": self.content_hash,
        }
        if legacy_id:
            payload["legacy_id"] = legacy_id
        return payload


def build_context_candidate_ref(
    *,
    kind: str,
    item_id: str,
    version: str = "v1",
    payload: Any = None,
) -> ContextCandidateRef:
    normalized_kind = _normalize_ref_part(kind, fallback="context")
    return ContextCandidateRef(
        kind=_KIND_ALIASES.get(normalized_kind, normalized_kind),
        item_id=_normalize_ref_part(item_id, fallback="item"),
        version=_normalize_ref_part(version, fallback="v1"),
        content_hash=_payload_hash(payload if payload is not None else item_id),
    )


def _json_safe_payload(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe_payload(child) for key, child in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_safe_payload(child) for child in value]
    return str(value)


def _string_refs(values: Iterable[Any] | None) -> list[str]:
    return [text for value in values or () if (text := str(value or "").strip())]


def build_metadata_activation_keys(
    *,
    candidate_kind: str,
    item_id: str,
    source_type: str,
    key_features: Mapping[str, Any] | None = None,
    value_pointer: Mapping[str, Any] | None = None,
    source_refs: Iterable[Any] | None = None,
    ref_kind: str | None = None,
    version: str = "metadata-v1",
    payload: Any = None,
) -> dict[str, Any]:
    safe_key_features = _json_safe_payload(dict(key_features or {}))
    safe_value_pointer = _json_safe_payload(dict(value_pointer or {}))
    ref_payload = payload if payload is not None else {
        "key_features": safe_key_features,
        "value_pointer": safe_value_pointer,
    }
    candidate_ref = build_context_candidate_ref(
        kind=ref_kind or candidate_kind,
        item_id=item_id,
        version=version,
        payload=ref_payload,
    ).to_manifest()
    candidate_ref["source_type"] = str(source_type or "metadata")
    return {
        "schema_version": "runtime.metadata_activation_keys.20260705",
        "candidate_kind": str(candidate_kind or "context"),
        "candidate_ref": candidate_ref,
        "key_features": safe_key_features,
        "value_pointer": safe_value_pointer,
        "source_refs": _string_refs(source_refs),
    }
