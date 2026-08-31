"""Read-only diagnostic commands for the unified command surface."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invocation_span import InvocationSpan
from app.models.runtime_task import RuntimeTask
from app.services.command_registry import build_default_command_registry

DIAGNOSTIC_COMMAND_NAMES = frozenset({"status", "usage", "cost", "stats", "context", "doctor", "version"})
_VERSION_PATH = Path(__file__).resolve().parents[2] / "VERSION"
_TOKEN_KEYS = ("input_tokens", "output_tokens", "cached_tokens", "total_tokens")
_COST_KEYS = ("cost_usd",)


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _read_version() -> str:
    try:
        return _VERSION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def _field(row: Any, name: str, index: int, default: Any = None) -> Any:
    if hasattr(row, name):
        return getattr(row, name)
    try:
        return row[index]
    except (IndexError, KeyError, TypeError):
        return default


def _all_rows(result: Any) -> list[Any]:
    all_method = getattr(result, "all", None)
    if callable(all_method):
        return list(all_method())
    scalars_method = getattr(result, "scalars", None)
    if callable(scalars_method):
        scalars = scalars_method()
        scalars_all = getattr(scalars, "all", None)
        if callable(scalars_all):
            return list(scalars_all())
    return []


def _first_numeric(payload: Any, keys: tuple[str, ...]) -> float:
    if not isinstance(payload, dict):
        return 0.0
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int | float):
            return float(value)
    return 0.0


def _canonical_usage(payload: Any) -> dict[str, float]:
    input_tokens = _first_numeric(payload, ("input_tokens", "prompt_tokens", "promptTokenCount"))
    output_tokens = _first_numeric(payload, ("output_tokens", "completion_tokens", "candidatesTokenCount"))
    cached_tokens = _first_numeric(payload, ("cached_tokens", "cached_input_tokens", "cache_read_input_tokens"))
    total_tokens = _first_numeric(payload, ("total_tokens", "total", "totalTokenCount"))
    if not total_tokens:
        total_tokens = input_tokens + output_tokens
    return {
        key: value
        for key, value in {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "total_tokens": total_tokens,
        }.items()
        if value
    }


def _canonical_cost(payload: Any) -> dict[str, float]:
    value = _first_numeric(payload, ("cost_usd", "estimated_cost_usd", "total_cost_usd"))
    return {"cost_usd": value} if value else {}


def _merge_totals(target: dict[str, float], increment: dict[str, float]) -> None:
    for key, value in increment.items():
        target[key] = target.get(key, 0.0) + value


async def _runtime_rows(db: AsyncSession, *, agent_id: uuid.UUID | None, session_id: str | None) -> list[Any]:
    if not hasattr(db, "execute"):
        return []
    stmt = select(RuntimeTask.id, RuntimeTask.task_type, RuntimeTask.status, RuntimeTask.token_usage).limit(500)
    if agent_id is not None:
        stmt = stmt.where(RuntimeTask.parent_agent_id == agent_id)
    if session_id:
        stmt = stmt.where(RuntimeTask.parent_session_id == str(session_id))
    result = await db.execute(stmt)
    return _all_rows(result)


async def _span_rows(db: AsyncSession, *, agent_id: uuid.UUID | None, session_id: str | None) -> list[Any]:
    if not hasattr(db, "execute"):
        return []
    stmt = select(
        InvocationSpan.runtime_task_id,
        InvocationSpan.span_type,
        InvocationSpan.status,
        InvocationSpan.duration_ms,
        InvocationSpan.usage,
    ).limit(500)
    if agent_id is not None:
        stmt = stmt.where(InvocationSpan.agent_id == agent_id)
    if session_id:
        stmt = stmt.where(InvocationSpan.session_id == str(session_id))
    result = await db.execute(stmt)
    return _all_rows(result)


def _build_snapshot(*, runtime_rows: list[Any], span_rows: list[Any]) -> dict[str, Any]:
    runtime_by_status: dict[str, int] = {}
    runtime_by_type: dict[str, int] = {}
    span_by_status: dict[str, int] = {}
    span_by_type: dict[str, int] = {}
    usage = {key: 0.0 for key in _TOKEN_KEYS}
    costs = {key: 0.0 for key in _COST_KEYS}
    task_usage_keys: dict[Any, set[str]] = {}
    task_cost_keys: dict[Any, set[str]] = {}
    duration_ms = 0.0

    for row in runtime_rows:
        task_id = _field(row, "id", 0)
        task_type = str(_field(row, "task_type", 1, "unknown") or "unknown")
        status = str(_field(row, "status", 2, "unknown") or "unknown")
        token_usage = _field(row, "token_usage", 3, {})
        task_usage = _canonical_usage(token_usage)
        task_cost = _canonical_cost(token_usage)
        runtime_by_type[task_type] = runtime_by_type.get(task_type, 0) + 1
        runtime_by_status[status] = runtime_by_status.get(status, 0) + 1
        _merge_totals(usage, task_usage)
        _merge_totals(costs, task_cost)
        if task_id is not None:
            task_usage_keys[task_id] = set(task_usage)
            task_cost_keys[task_id] = set(task_cost)

    for row in span_rows:
        runtime_task_id = _field(row, "runtime_task_id", 0)
        span_type = str(_field(row, "span_type", 1, "unknown") or "unknown")
        status = str(_field(row, "status", 2, "unknown") or "unknown")
        duration_ms += float(_field(row, "duration_ms", 3, 0.0) or 0.0)
        span_usage = _field(row, "usage", 4, {})
        covered_usage = task_usage_keys.get(runtime_task_id, set())
        covered_cost = task_cost_keys.get(runtime_task_id, set())
        span_by_type[span_type] = span_by_type.get(span_type, 0) + 1
        span_by_status[status] = span_by_status.get(status, 0) + 1
        _merge_totals(
            usage, {key: value for key, value in _canonical_usage(span_usage).items() if key not in covered_usage}
        )
        _merge_totals(
            costs, {key: value for key, value in _canonical_cost(span_usage).items() if key not in covered_cost}
        )

    return {
        "runtime_tasks": {
            "count": len(runtime_rows),
            "by_status": runtime_by_status,
            "by_type": runtime_by_type,
        },
        "invocation_spans": {
            "count": len(span_rows),
            "by_status": span_by_status,
            "by_type": span_by_type,
            "duration_ms": round(duration_ms, 3),
        },
        "usage": {key: int(value) if value.is_integer() else value for key, value in usage.items() if value},
        "cost": {key: round(value, 6) for key, value in costs.items() if value},
    }


def _issues(snapshot: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if not _read_version() or _read_version() == "unknown":
        issues.append("version_file_missing")
    runtime_status = snapshot["runtime_tasks"]["by_status"]
    span_status = snapshot["invocation_spans"]["by_status"]
    if runtime_status.get("failed", 0) or runtime_status.get("needs_reconciliation", 0):
        issues.append("runtime_task_failures_present")
    if span_status.get("error", 0) or span_status.get("failed", 0):
        issues.append("invocation_span_errors_present")
    return issues


async def execute_diagnostic_command(
    *,
    db: AsyncSession,
    agent: Any,
    user: Any,
    command_name: str,
    session_id: str | None,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a source-backed diagnostic command without side effects."""
    del user, arguments
    name = str(command_name or "").strip()
    if name not in DIAGNOSTIC_COMMAND_NAMES:
        raise ValueError(f"Unsupported diagnostic command {command_name!r}")

    agent_id = _coerce_uuid(getattr(agent, "id", None))
    runtime = await _runtime_rows(db, agent_id=agent_id, session_id=session_id)
    spans = await _span_rows(db, agent_id=agent_id, session_id=session_id)
    snapshot = _build_snapshot(runtime_rows=runtime, span_rows=spans)
    registry = build_default_command_registry(include_optional_coding_pack=True)
    issues = _issues(snapshot)
    base = {
        "ok": True,
        "command": name,
        "read_only": True,
        "agent_id": str(agent_id) if agent_id else None,
        "session_id": str(session_id) if session_id else None,
        "sources": ["backend/VERSION", "command_registry", "runtime_tasks", "invocation_spans"],
    }

    if name == "version":
        return {**base, "version": _read_version()}
    if name == "usage":
        return {
            **base,
            "usage": snapshot["usage"],
            "cost": snapshot["cost"],
            "ui_action": {
                "type": "open_usage_panel",
                "session_id": session_id,
                "message": "Session usage is ready.",
            },
        }
    if name == "cost":
        return {**base, "cost": snapshot["cost"], "currency": "USD"}
    if name == "stats":
        return {
            **base,
            "command_count": len(registry.values()),
            "runtime_tasks": snapshot["runtime_tasks"],
            "invocation_spans": snapshot["invocation_spans"],
        }
    if name == "context":
        return {
            **base,
            "transcript_truth": "chat_session_t0",
            "trace_truth": "invocation_spans",
            "context_ladder": [
                "tool_result_eviction",
                "round_tool_result_budget",
                "microcompact",
                "autocompact",
                "reactive_prompt_too_long_retry",
            ],
            "ui_action": {
                "type": "open_context_panel",
                "session_id": session_id,
                "message": "Session context is ready.",
            },
        }
    if name == "doctor":
        return {
            **base,
            "health": "ok" if not issues else "needs_attention",
            "issues": issues,
            "runtime_tasks": snapshot["runtime_tasks"],
            "invocation_spans": snapshot["invocation_spans"],
        }
    return {
        **base,
        "health": "ok" if not issues else "needs_attention",
        "version": _read_version(),
        "issues": issues,
        "runtime_tasks": snapshot["runtime_tasks"],
        "invocation_spans": snapshot["invocation_spans"],
    }
