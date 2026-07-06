"""Unified context-candidate reference identifiers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any


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
