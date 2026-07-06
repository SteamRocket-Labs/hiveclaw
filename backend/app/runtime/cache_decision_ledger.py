"""Prompt/cache decision ledger helpers."""

from __future__ import annotations

import hashlib
from typing import Any


def _hash_cache_key(cache_key: str) -> str:
    return hashlib.sha256(str(cache_key or "").encode("utf-8")).hexdigest()[:16]


def build_cache_decision_entry(
    *,
    cache_surface: str,
    cache_key: str = "",
    decision: str,
    invalidation_reason: str = "",
    shared_with_parent: bool = False,
) -> dict[str, Any]:
    return {
        "schema": "hive.ccplus.cache_decision.v1",
        "cache_surface": str(cache_surface or "none"),
        "cache_key": "[redacted]" if cache_key else "",
        "cache_key_hash": _hash_cache_key(cache_key) if cache_key else "",
        "decision": str(decision or "none"),
        "invalidation_reason": str(invalidation_reason or ""),
        "shared_with_parent": bool(shared_with_parent),
    }


def append_cache_decision_entry(session_context: Any | None, entry: dict[str, Any], *, limit: int = 50) -> None:
    if session_context is None:
        return
    from app.runtime.context import ensure_runtime_assembly_state

    ensure_runtime_assembly_state(session_context).record_cache_decision(entry, limit=limit)
