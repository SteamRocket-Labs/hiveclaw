"""Unified runtime decision ledger helpers."""

from __future__ import annotations

from typing import Any


def build_runtime_decision_entry(
    *,
    kind: str,
    status: str,
    trigger: str = "",
    reason: str = "",
    next_action: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "hive.ccplus.runtime_decision.v1",
        "kind": str(kind or "unknown"),
        "trigger": str(trigger or ""),
        "status": str(status or "unknown"),
        "reason": str(reason or ""),
        "next_action": str(next_action or ""),
        "details": dict(details or {}),
    }


def append_runtime_decision_entry(session_context: Any | None, entry: dict[str, Any], *, limit: int = 100) -> None:
    if session_context is None:
        return
    from app.runtime.context import ensure_runtime_assembly_state

    ensure_runtime_assembly_state(session_context).record_runtime_decision(entry, limit=limit)
