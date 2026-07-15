from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_budget_unavailable_guard_blocks_amplifying_tool_before_handler(monkeypatch) -> None:
    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest
    from app.tools.result_envelope import ToolContentEnvelope

    handler_calls: list[str] = []

    async def fake_execute(tool_name, *_args, **_kwargs):
        handler_calls.append(tool_name)
        return "executed"

    monkeypatch.setattr(invoker, "_execute_tool_with_request", fake_execute)
    session_context = SimpleNamespace(
        metadata={
            "runtime_budget": {
                "schema": "hive.runtime_budget_binding.v1",
                "status": "unavailable",
                "reason": "interactive_direct_response_budget_service_unavailable",
                "retryable": True,
                "interactive": True,
                "work_amplifying_tools_disabled": True,
            }
        }
    )
    kernel = invoker.get_agent_kernel(
        AgentInvocationRequest(
            model=SimpleNamespace(),
            messages=[],
            agent_name="Agent",
            role_description="",
            agent_id=uuid4(),
            session_context=session_context,
        )
    )

    result = await kernel._deps.execute_tool(
        "spawn_subagent",
        {"task": "fan out"},
        SimpleNamespace(session_context=session_context),
        lambda _event: None,
    )

    assert isinstance(result, ToolContentEnvelope)
    assert result.metadata == {
        "status": "unavailable",
        "code": "runtime_budget_service_unavailable",
        "reason": "interactive_direct_response_budget_service_unavailable",
        "retryable": True,
        "effect_started": False,
        "tool_name": "spawn_subagent",
    }
    assert "<tool_error>" in result.text
    assert handler_calls == []


@pytest.mark.asyncio
async def test_budget_unavailable_guard_leaves_non_amplifying_tool_available(monkeypatch) -> None:
    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest

    handler_calls: list[str] = []

    async def fake_execute(tool_name, *_args, **_kwargs):
        handler_calls.append(tool_name)
        return "read result"

    monkeypatch.setattr(invoker, "_execute_tool_with_request", fake_execute)
    session_context = SimpleNamespace(
        metadata={
            "runtime_budget": {
                "schema": "hive.runtime_budget_binding.v1",
                "status": "unavailable",
                "work_amplifying_tools_disabled": True,
            }
        }
    )
    kernel = invoker.get_agent_kernel(
        AgentInvocationRequest(
            model=SimpleNamespace(),
            messages=[],
            agent_name="Agent",
            role_description="",
            agent_id=uuid4(),
            session_context=session_context,
        )
    )

    result = await kernel._deps.execute_tool(
        "read_file",
        {"path": "workspace/a.md"},
        SimpleNamespace(session_context=session_context),
        lambda _event: None,
    )

    assert result == "read result"
    assert handler_calls == ["read_file"]


@pytest.mark.asyncio
async def test_budget_unavailable_guard_allows_exact_wakeup_stop_but_blocks_new_wakeup(monkeypatch) -> None:
    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest
    from app.tools.result_envelope import ToolContentEnvelope

    handler_calls: list[dict] = []

    async def fake_execute(_tool_name, args, *_rest, **_kwargs):
        handler_calls.append(args)
        return "stopped"

    monkeypatch.setattr(invoker, "_execute_tool_with_request", fake_execute)
    session_context = SimpleNamespace(
        metadata={
            "runtime_budget": {
                "schema": "hive.runtime_budget_binding.v1",
                "status": "unavailable",
                "reason": "interactive_direct_response_budget_service_unavailable",
                "work_amplifying_tools_disabled": True,
            }
        }
    )
    kernel = invoker.get_agent_kernel(
        AgentInvocationRequest(
            model=SimpleNamespace(),
            messages=[],
            agent_name="Agent",
            role_description="",
            agent_id=uuid4(),
            session_context=session_context,
        )
    )

    stopped = await kernel._deps.execute_tool(
        "schedule_wakeup",
        {"stop": True},
        SimpleNamespace(session_context=session_context),
        lambda _event: None,
    )
    blocked = await kernel._deps.execute_tool(
        "schedule_wakeup",
        {"delay_seconds": 60, "prompt": "continue"},
        SimpleNamespace(session_context=session_context),
        lambda _event: None,
    )

    assert stopped == "stopped"
    assert isinstance(blocked, ToolContentEnvelope)
    assert handler_calls == [{"stop": True}]
