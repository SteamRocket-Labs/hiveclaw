"""Context candidates for transient runtime reminders.

Runtime reminders are intentionally injected into the current provider request
only; they must not be persisted as conversation messages. This module gives the
same transient text a structured context-candidate trail so `/context` and
workbench projections can explain where the reminder came from.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.runtime.context_candidates import build_context_candidate_ref


def _content_hash(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        text = str(value or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _priority(value: int | str | None) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 50


def build_runtime_reminder_candidate(
    *,
    source: str,
    text: str,
    ttl: str,
    priority: int | str | None,
    consumed_at: str | None,
    item_id: str | None = None,
    reason: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_name = str(source or "runtime_reminder").strip() or "runtime_reminder"
    reminder_text = str(text or "")
    candidate_payload = {
        "source": source_name,
        "text": reminder_text,
        "ttl": str(ttl or "current_round"),
        "priority": _priority(priority),
        "consumed_at": consumed_at,
        "payload": dict(payload or {}),
    }
    candidate_ref = build_context_candidate_ref(
        kind="runtime_reminder",
        item_id=item_id or source_name,
        version=str(ttl or "current_round"),
        payload=candidate_payload,
    ).to_manifest()
    content_hash = _content_hash(candidate_payload)
    return {
        "schema": "hive.ccplus.context_candidate.v1",
        "id": f"ctx:runtime_reminder:{source_name}:{content_hash}",
        "candidate_ref": candidate_ref,
        "kind": "runtime_reminder",
        "name": source_name,
        "source": source_name,
        "source_ref": source_name,
        "source_hash": content_hash,
        "chars": len(reminder_text),
        "tokens": _estimate_tokens(reminder_text),
        "selected": True,
        "why_selected": reason or "runtime controller selected this transient reminder for the current context",
        "suppressed_reason": "",
        "score": min(1.0, max(0.0, _priority(priority) / 100)),
        "cacheability": "transient",
        "budget_key": "runtime_reminder",
        "budget_chars": len(reminder_text),
        "ttl": str(ttl or "current_round"),
        "priority": _priority(priority),
        "consumed_at": consumed_at,
        "content": reminder_text,
        "content_preview": reminder_text[:500],
        "payload": dict(payload or {}),
    }


def append_runtime_reminder_candidate(
    session_context: Any | None,
    *,
    source: str,
    text: str,
    ttl: str,
    priority: int | str | None,
    consumed_at: str | None,
    item_id: str | None = None,
    reason: str | None = None,
    payload: dict[str, Any] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    candidate = build_runtime_reminder_candidate(
        source=source,
        text=text,
        ttl=ttl,
        priority=priority,
        consumed_at=consumed_at,
        item_id=item_id,
        reason=reason,
        payload=payload,
    )
    if session_context is None:
        return candidate

    from app.runtime.context import ensure_runtime_assembly_state

    ensure_runtime_assembly_state(session_context).record_runtime_reminder_candidate(candidate, limit=limit)
    return candidate


__all__ = [
    "append_runtime_reminder_candidate",
    "build_runtime_reminder_candidate",
]
