"""Tool failure telemetry aggregation for production troubleshooting."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity_log import AgentActivityLog


def _top_counts(counter: Counter, key_name: str) -> list[dict]:
    return [{key_name: key, "count": count} for key, count in counter.most_common()]


def summarize_tool_failure_logs(logs: list) -> dict:
    error_logs = [
        log
        for log in logs
        if getattr(log, "action_type", None) == "error" and isinstance(getattr(log, "detail_json", None), dict)
    ]

    by_tool: Counter = Counter()
    by_provider: Counter = Counter()
    by_error_class: Counter = Counter()
    by_http_status: Counter = Counter()
    recent_errors: list[dict] = []

    ordered_errors = sorted(
        error_logs,
        key=lambda log: getattr(log, "created_at", None) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )

    for log in ordered_errors:
        detail = log.detail_json or {}
        tool_name = detail.get("tool_name") or detail.get("tool")
        provider = detail.get("provider")
        error_class = detail.get("error_class")
        http_status = detail.get("http_status")

        if tool_name:
            by_tool[str(tool_name)] += 1
        if provider:
            by_provider[str(provider)] += 1
        if error_class:
            by_error_class[str(error_class)] += 1
        if http_status is not None:
            by_http_status[int(http_status)] += 1

        recent_errors.append(
            {
                "summary": getattr(log, "summary", ""),
                "tool_name": tool_name,
                "provider": provider,
                "error_class": error_class,
                "http_status": http_status,
                "retryable": detail.get("retryable"),
                "created_at": (
                    log.created_at.isoformat()
                    if getattr(log, "created_at", None)
                    else None
                ),
            }
        )

    return {
        "total_errors": len(error_logs),
        "by_tool": _top_counts(by_tool, "tool_name"),
        "by_provider": _top_counts(by_provider, "provider"),
        "by_error_class": _top_counts(by_error_class, "error_class"),
        "by_http_status": _top_counts(by_http_status, "http_status"),
        "recent_errors": recent_errors[:20],
    }


async def collect_agent_tool_failure_summary(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    hours: int = 24,
    limit: int = 500,
) -> dict:
    since = datetime.now(UTC) - timedelta(hours=max(hours, 1))
    result = await db.execute(
        select(AgentActivityLog)
        .where(AgentActivityLog.agent_id == agent_id)
        .where(AgentActivityLog.action_type == "error")
        .where(AgentActivityLog.created_at >= since)
        .order_by(AgentActivityLog.created_at.desc())
        .limit(limit)
    )
    logs = result.scalars().all()
    return summarize_tool_failure_logs(logs)
