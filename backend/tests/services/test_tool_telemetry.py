from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_summarize_tool_failure_logs_groups_by_tool_provider_and_error_class() -> None:
    from app.services.tool_telemetry import summarize_tool_failure_logs

    now = datetime.now(UTC)
    logs = [
        SimpleNamespace(
            action_type="error",
            summary="Tool firecrawl_fetch failed",
            detail_json={
                "tool_name": "firecrawl_fetch",
                "provider": "firecrawl",
                "error_class": "quota_or_billing",
                "http_status": 402,
                "retryable": False,
            },
            created_at=now,
        ),
        SimpleNamespace(
            action_type="error",
            summary="Tool firecrawl_fetch failed again",
            detail_json={
                "tool_name": "firecrawl_fetch",
                "provider": "firecrawl",
                "error_class": "quota_or_billing",
                "http_status": 402,
                "retryable": False,
            },
            created_at=now - timedelta(minutes=5),
        ),
        SimpleNamespace(
            action_type="error",
            summary="Tool web_fetch timed out",
            detail_json={
                "tool_name": "web_fetch",
                "provider": "web_fetch",
                "error_class": "timeout",
                "retryable": True,
            },
            created_at=now - timedelta(minutes=10),
        ),
        SimpleNamespace(
            action_type="tool_call",
            summary="Called tool read_file",
            detail_json={"tool": "read_file"},
            created_at=now - timedelta(minutes=15),
        ),
    ]

    summary = summarize_tool_failure_logs(logs)

    assert summary["total_errors"] == 3
    assert summary["by_tool"][0] == {"tool_name": "firecrawl_fetch", "count": 2}
    assert summary["by_provider"][0] == {"provider": "firecrawl", "count": 2}
    assert summary["by_error_class"][0] == {"error_class": "quota_or_billing", "count": 2}
    assert summary["by_http_status"][0] == {"http_status": 402, "count": 2}
    assert summary["recent_errors"][0]["tool_name"] == "firecrawl_fetch"


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)


class _FakeDB:
    def __init__(self, values):
        self._values = list(values)

    async def execute(self, _stmt):
        if not self._values:
            raise AssertionError("Unexpected execute() call")
        return _ListResult(self._values.pop(0))


@pytest.mark.asyncio
async def test_collect_agent_tool_failure_summary_queries_error_logs() -> None:
    from app.services.tool_telemetry import collect_agent_tool_failure_summary

    agent_id = uuid4()
    now = datetime.now(UTC)
    logs = [
        SimpleNamespace(
            action_type="error",
            summary="Tool web_search failed",
            detail_json={
                "tool_name": "web_search",
                "provider": "duckduckgo",
                "error_class": "provider_unavailable",
                "http_status": 503,
                "retryable": True,
            },
            created_at=now,
        )
    ]
    db = _FakeDB([logs])

    summary = await collect_agent_tool_failure_summary(db, agent_id=agent_id, hours=24, limit=100)

    assert summary["total_errors"] == 1
    assert summary["by_tool"] == [{"tool_name": "web_search", "count": 1}]
