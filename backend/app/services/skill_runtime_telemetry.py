from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.runtime.session import SessionContext
from app.services.llm_error_policy import is_llm_error_message
from app.services.skill_lifecycle import record_skill_runtime_usage

logger = logging.getLogger(__name__)

_OUTCOME_RE = re.compile(r"\[OUTCOME:\s*(noop|action_taken|curated|failure|crash)\s*\]", re.IGNORECASE)


def _tool_event_args(data: dict[str, Any]) -> dict[str, Any]:
    raw_args = data.get("args")
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _append_unique(values: list[str], value: str) -> None:
    normalized = value.strip()
    if normalized and normalized not in values:
        values.append(normalized)


def _collect_skill_usage(tool_events: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    loaded_skill_names: list[str] = []
    tool_names: list[str] = []
    for event in tool_events:
        if event.get("status") not in {None, "done"}:
            continue
        tool_name = str(event.get("name") or "").strip()
        if not tool_name:
            continue
        tool_names.append(tool_name)
        if tool_name != "load_skill":
            continue
        args = _tool_event_args(event)
        skill_name = str(args.get("name") or args.get("skill_name") or args.get("skill") or args.get("query") or "")
        _append_unique(loaded_skill_names, skill_name)
    return loaded_skill_names, tool_names


def _skill_usage_status(*, terminal_status: str, assistant_text: str) -> str:
    if is_llm_error_message(assistant_text):
        return "failed"

    outcome_match = _OUTCOME_RE.search(assistant_text or "")
    if outcome_match:
        outcome = outcome_match.group(1).lower()
        if outcome in {"action_taken", "curated"}:
            return "success"
        if outcome in {"failure", "crash"}:
            return "failed"
        if outcome == "noop":
            return "noop"

    normalized = str(terminal_status or "").strip().lower()
    # Terminal runtime state proves only that the invocation lifecycle ended.
    # It cannot decide whether a loaded Skill was useful or worthy of change.
    # Semantic status must come from the model-authored OUTCOME declaration.
    if normalized in {"failed", "failure", "error", "killed", "cancelled", "canceled", "timeout", "timed_out"}:
        return "failed"
    if normalized == "noop":
        return "noop"
    return "unknown"


def _session_metadata(session_context: SessionContext | None) -> dict[str, Any]:
    metadata = session_context.metadata if session_context is not None else None
    return metadata if isinstance(metadata, dict) else {}


def record_skill_runtime_usage_for_invocation(
    *,
    agent_id: uuid.UUID | str | None,
    session_context: SessionContext | None,
    tool_events: list[dict[str, Any]],
    terminal_status: str,
    assistant_text: str,
    note: str,
) -> dict[str, Any] | None:
    """Record skill usage from a completed invocation without coupling callers to lifecycle files."""

    if agent_id is None:
        return None

    loaded_skill_names, tool_names = _collect_skill_usage(tool_events)
    if not loaded_skill_names:
        return None

    metadata = _session_metadata(session_context)
    source = str(getattr(session_context, "source", "") or metadata.get("source") or "runtime").strip() or "runtime"
    session_id = getattr(session_context, "session_id", None) if session_context is not None else None
    status = _skill_usage_status(terminal_status=terminal_status, assistant_text=assistant_text)
    clean_note = (note or assistant_text or "").strip()
    blocker = clean_note if status == "failed" else None
    workspace = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)

    try:
        return record_skill_runtime_usage(
            workspace,
            skill_name=loaded_skill_names[0],
            loaded_skill_names=loaded_skill_names,
            tool_names=tool_names,
            status=status,
            note=clean_note,
            source=source,
            session_id=str(session_id) if session_id else None,
            runtime_task_id=str(metadata.get("runtime_task_id") or metadata.get("task_id") or "").strip() or None,
            trace_id=str(metadata.get("trace_id") or "").strip() or None,
            blocker=blocker,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never break agent execution
        logger.warning(
            "[SkillRuntimeTelemetry] record failed: agent_id=%s source=%s session_id=%s error=%s",
            agent_id,
            source,
            session_id,
            exc,
        )
        return None
