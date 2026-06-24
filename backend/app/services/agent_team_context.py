"""Prompt-facing team context for subagent/workflow coordination.

This renderer surfaces Session/T0-backed coordination context. RuntimeTask and
CoordinationSignal are read/execution models used to find active work and
mailbox updates; they are not transcript truth.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.coordination import CoordinationSignal
from app.models.runtime_task import RuntimeTask

_ACTIVE_STATUSES = {"pending", "running", "in_progress", "blocked", "suspended"}
_TEAM_TASK_TYPES = {"subagent", "workflow", "delegation"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _short(value: Any, limit: int = 220) -> str:
    text = " ".join(_clean(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _id(value: Any) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    return _clean(value)


def _task_line(task: dict[str, Any]) -> str:
    task_id = _id(task.get("id") or task.get("task_id"))[:8]
    task_type = _clean(task.get("task_type")) or "task"
    status = _clean(task.get("status")) or "unknown"
    teammate = _clean(task.get("child_agent_name") or task.get("child_agent_id") or task.get("name")) or "teammate"
    summary = _short(task.get("result_summary") or task.get("prompt") or task.get("summary"))
    suffix = f" — {summary}" if summary else ""
    return f"- {task_type} {task_id} [{status}] {teammate}{suffix}"


def _signal_line(signal: dict[str, Any]) -> str:
    signal_type = _clean(signal.get("signal_type")) or "signal"
    sender = _clean(signal.get("from_agent_id")) or "teammate"
    thread = _clean(signal.get("thread_id"))
    content = _short(signal.get("content"))
    thread_suffix = f" thread={thread[:8]}" if thread else ""
    return f"- [{signal_type}]{thread_suffix} from {sender}: {content}"


def render_team_context_block(
    *,
    tasks: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    signals: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    task_limit: int = 8,
    signal_limit: int = 8,
) -> str:
    task_rows = [task for task in (tasks or []) if isinstance(task, dict)]
    signal_rows = [signal for signal in (signals or []) if isinstance(signal, dict)]
    if not task_rows and not signal_rows:
        return ""

    lines: list[str] = []
    if task_rows:
        lines.append("## Team Context")
        lines.append("Current teammate/workflow state projected from Session/T0-backed runtime records:")
        for task in task_rows[: max(1, task_limit)]:
            lines.append(_task_line(task))
        if len(task_rows) > task_limit:
            lines.append(f"- ... {len(task_rows) - task_limit} more active/recent team task(s)")

    if signal_rows:
        if lines:
            lines.append("")
        lines.append("## Teammate Mailbox")
        lines.append(
            "Unread durable coordination signals; treat completion notices as next-turn context, "
            "not as a repeated status check:"
        )
        for signal in signal_rows[: max(1, signal_limit)]:
            lines.append(_signal_line(signal))
        if len(signal_rows) > signal_limit:
            lines.append(f"- ... {len(signal_rows) - signal_limit} more mailbox signal(s)")

    return "\n".join(lines).strip()


def _runtime_task_to_dict(task: RuntimeTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "child_agent_id": str(task.child_agent_id) if task.child_agent_id else "",
        "child_agent_name": task.child_agent_name,
        "prompt": task.prompt,
        "result_summary": task.result_summary,
        "parent_session_id": task.parent_session_id,
        "child_session_id": task.child_session_id,
    }


def _signal_to_dict(signal: CoordinationSignal) -> dict[str, Any]:
    return {
        "id": signal.id,
        "from_agent_id": signal.from_agent_id,
        "to_agent_id": signal.to_agent_id,
        "content": signal.content,
        "signal_type": signal.signal_type,
        "thread_id": signal.thread_id,
    }


async def build_prompt_facing_team_context(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | str | None,
    session_id: str | uuid.UUID | None = None,
    task_limit: int = 8,
    signal_limit: int = 8,
) -> str:
    """Build the CC-style team context/mailbox block for the current agent turn."""

    if not tenant_id:
        return ""
    session_key = _clean(session_id)
    async with tenant_scoped_session(tenant_id) as db:
        task_stmt = (
            select(RuntimeTask)
            .where(RuntimeTask.parent_agent_id == agent_id, RuntimeTask.task_type.in_(tuple(_TEAM_TASK_TYPES)))
            .order_by(RuntimeTask.created_at.desc())
            .limit(max(1, task_limit))
        )
        if session_key:
            task_stmt = task_stmt.where(
                (RuntimeTask.parent_session_id == session_key) | (RuntimeTask.child_session_id == session_key)
            )
        tasks = [
            _runtime_task_to_dict(task)
            for task in (await db.execute(task_stmt)).scalars().all()
            if _clean(task.status) in _ACTIVE_STATUSES or _clean(task.result_summary)
        ]

        signal_stmt = (
            select(CoordinationSignal)
            .where(CoordinationSignal.to_agent_id == str(agent_id))
            .order_by(CoordinationSignal.created_at.desc())
            .limit(max(1, signal_limit))
        )
        if session_key:
            signal_stmt = signal_stmt.where(CoordinationSignal.thread_id == session_key)
        signals = [_signal_to_dict(signal) for signal in (await db.execute(signal_stmt)).scalars().all()]

    return render_team_context_block(tasks=tasks, signals=signals, task_limit=task_limit, signal_limit=signal_limit)
