"""Decision entry for background subagent completion and reconciliation."""

from __future__ import annotations

from typing import Any

SUBAGENT_DECISION_ENTRY_SCHEMA = "hive.ccplus.subagent_decision_entry.v1"


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _default_required_user_action(
    *,
    status: str,
    blocker: str | None,
    retry_available: bool,
) -> str:
    if status == "needs_reconciliation":
        if retry_available:
            return "approve_reconciliation_retry"
        return "manual_reconcile_or_abandon"
    if status in {"failed", "killed", "cancelled", "canceled"}:
        return "inspect_failure_and_decide_retry"
    if blocker:
        return "inspect_blocker"
    return "observe_result"


def build_subagent_decision_entry(
    *,
    run_id: Any,
    status: Any,
    subagent_name: Any = None,
    subagent_type: Any = None,
    replay_mode: Any = None,
    blocker: Any = None,
    safe_to_retry: bool | None = None,
    retry_available: bool | None = None,
    required_user_action: str | None = None,
    child_session_id: Any = None,
    parent_session_id: Any = None,
    summary: Any = None,
    source: str = "subagent_runtime",
) -> dict[str, Any]:
    normalized_status = str(status or "").strip().lower() or "unknown"
    normalized_blocker = _text(blocker)
    replay_mode_text = _text(replay_mode) or ("blocked" if normalized_status == "needs_reconciliation" else "none")
    retry_available_value = bool(retry_available)
    if safe_to_retry is None:
        safe_to_retry = normalized_status in {"completed", "succeeded", "success"} and not normalized_blocker
    action = required_user_action or _default_required_user_action(
        status=normalized_status,
        blocker=normalized_blocker,
        retry_available=retry_available_value,
    )
    return {
        "schema": SUBAGENT_DECISION_ENTRY_SCHEMA,
        "run_id": _text(run_id),
        "status": normalized_status,
        "subagent_name": _text(subagent_name),
        "subagent_type": _text(subagent_type),
        "replay_mode": replay_mode_text,
        "blocker": normalized_blocker,
        "safe_to_retry": bool(safe_to_retry),
        "retry_available": retry_available_value,
        "required_user_action": action,
        "child_session_id": _text(child_session_id),
        "parent_session_id": _text(parent_session_id),
        "summary": _text(summary),
        "source": source,
    }


def subagent_decision_entry_from_metadata(
    metadata: dict[str, Any] | None,
    *,
    run_id: Any,
    status: Any,
    child_session_id: Any = None,
    parent_session_id: Any = None,
    summary: Any = None,
) -> dict[str, Any]:
    data = dict(metadata or {})
    existing = data.get("subagent_decision_entry")
    if isinstance(existing, dict) and existing.get("schema") == SUBAGENT_DECISION_ENTRY_SCHEMA:
        return {
            **existing,
            "run_id": _text(run_id) or existing.get("run_id"),
            "status": str(status or existing.get("status") or "").strip().lower() or "unknown",
            "child_session_id": _text(child_session_id) or existing.get("child_session_id"),
            "parent_session_id": _text(parent_session_id) or existing.get("parent_session_id"),
            "summary": _text(summary) or existing.get("summary"),
        }
    return build_subagent_decision_entry(
        run_id=run_id,
        status=status,
        subagent_name=data.get("subagent_name"),
        subagent_type=data.get("subagent_type"),
        replay_mode=data.get("restart_resume_mode"),
        blocker=data.get("restart_resume_blocker") or data.get("reconciliation_reason"),
        safe_to_retry=data.get("safe_to_retry") if isinstance(data.get("safe_to_retry"), bool) else None,
        retry_available=bool(data.get("reconciliation_retry_allowed")),
        child_session_id=child_session_id or data.get("child_session_id"),
        parent_session_id=parent_session_id or data.get("parent_session_id"),
        summary=summary,
    )
