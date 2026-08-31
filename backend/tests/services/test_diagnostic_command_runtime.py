from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, *batches) -> None:
        self._batches = list(batches)
        self.executes = 0

    async def execute(self, _stmt):
        self.executes += 1
        return _Rows(self._batches.pop(0) if self._batches else [])


@pytest.mark.asyncio
async def test_usage_diagnostic_reads_runtime_tasks_and_invocation_spans():
    from app.services.diagnostic_command_runtime import execute_diagnostic_command

    agent_id = uuid4()
    runtime_task_id = uuid4()
    db = _FakeDB(
        [
            SimpleNamespace(
                id=runtime_task_id,
                task_type="web_chat_turn",
                status="completed",
                token_usage={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
            )
        ],
        [
            SimpleNamespace(
                runtime_task_id=runtime_task_id,
                span_type="generation",
                status="ok",
                duration_ms=20.5,
                usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7, "cost_usd": 0.01},
            )
        ],
    )

    result = await execute_diagnostic_command(
        db=db,
        agent=SimpleNamespace(id=agent_id, tenant_id=uuid4()),
        user=SimpleNamespace(id=uuid4()),
        command_name="usage",
        session_id="session-1",
        arguments={},
    )

    assert result["ok"] is True
    assert result["command"] == "usage"
    assert result["usage"]["total_tokens"] == 12
    assert result["usage"]["input_tokens"] == 5
    assert result["usage"]["output_tokens"] == 7
    assert result["cost"]["cost_usd"] == 0.01
    assert result["ui_action"] == {
        "type": "open_usage_panel",
        "session_id": "session-1",
        "message": "Session usage is ready.",
    }
    assert "runtime_tasks" in result["sources"]
    assert "invocation_spans" in result["sources"]


@pytest.mark.asyncio
async def test_doctor_diagnostic_reports_registry_and_runtime_issues():
    from app.services.diagnostic_command_runtime import execute_diagnostic_command

    result = await execute_diagnostic_command(
        db=_FakeDB(
            [SimpleNamespace(task_type="advanced_plan", status="failed", token_usage={})],
            [SimpleNamespace(span_type="tool", status="error", duration_ms=3, usage={})],
        ),
        agent=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
        user=SimpleNamespace(id=uuid4()),
        command_name="doctor",
        session_id=None,
        arguments={},
    )

    assert result["ok"] is True
    assert result["health"] == "needs_attention"
    assert "runtime_task_failures_present" in result["issues"]
    assert "invocation_span_errors_present" in result["issues"]


@pytest.mark.asyncio
async def test_context_diagnostic_reports_only_live_context_ladder():
    from app.services.diagnostic_command_runtime import execute_diagnostic_command

    result = await execute_diagnostic_command(
        db=_FakeDB([], []),
        agent=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
        user=SimpleNamespace(id=uuid4()),
        command_name="context",
        session_id="session-ctx",
        arguments={},
    )

    assert result["context_ladder"] == [
        "tool_result_eviction",
        "round_tool_result_budget",
        "microcompact",
        "autocompact",
        "reactive_prompt_too_long_retry",
    ]
    assert "snip_or_evict" not in result["context_ladder"]
    assert "read_time_projection_collapse" not in result["context_ladder"]
    assert "blocking_limit" not in result["context_ladder"]
    assert "reactive_compact" not in result["context_ladder"]
    assert result["ui_action"] == {
        "type": "open_context_panel",
        "session_id": "session-ctx",
        "message": "Session context is ready.",
    }
