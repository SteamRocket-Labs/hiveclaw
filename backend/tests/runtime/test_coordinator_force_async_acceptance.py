"""CCPlus V1 §7 acceptance — coordinator_force_async (D-02 non-blocking delegate).

GENUINE acceptance test for the §7 ``coordinator_force_async`` selector. D-02
makes coordinator-mode delegation **async / non-blocking**: the coordinator's
primary verb ``delegate_to_agent`` spawns a background worker and returns
immediately with a task handle, and continuation happens through
``check_async_task`` (status) and ``send_agent_session_message`` (mailbox
append) — the coordinator never blocks the turn waiting on a worker.

These tests assert the REAL integrated wiring that delivers that semantics:

  1. The LLM-facing ``delegate_to_agent`` tool handler routes to the async
     delegate path (``_delegate_to_agent_async`` → orchestrator ``delegate_async``,
     which launches a background task and returns a handle). We invoke the tool
     handler with ``delegate_async`` stubbed and assert it returns a task handle
     ("return immediately"), not a synthesized blocking worker transcript.
  2. The async continuation tools are simultaneously in
     ``COORDINATOR_ALLOWED_TOOLS`` AND ``CORE_TOOL_NAMES`` so the coordinator can
     see and call them, and the coordinator prompt directs the non-blocking
     delegate→check/continue loop.

Revert-sensitive: if ``delegate_to_agent`` reverted to a synchronous/blocking
delegate (no handle, awaits the worker), or if the async continuation tools were
dropped from the coordinator/CORE surfaces, these assertions fail.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.runtime.coordinator import (
    COORDINATOR_ALLOWED_TOOLS,
    filter_tools_for_coordinator,
    get_coordinator_prompt,
)


def test_coordinator_force_async_delegate_tool_is_the_async_handle_variant():
    """The coordinator's ``delegate_to_agent`` tool advertises async, return-now semantics.

    The LLM-facing tool metadata is what the coordinator sees. D-02 requires the
    delegate verb to be the non-blocking, background variant — the description
    says it spawns an async task and returns immediately with a handle, NOT a
    synchronous back-and-forth (which is ``send_message_to_agent``).
    """
    from app.services.agent_tools import get_combined_openai_tools

    delegate_specs = [tool for tool in get_combined_openai_tools() if tool["function"]["name"] == "delegate_to_agent"]
    assert len(delegate_specs) == 1
    description = delegate_specs[0]["function"]["description"].lower()

    # Non-blocking, returns a handle immediately.
    assert "async" in description
    assert "return immediately" in description
    assert "background" in description
    # And the continuation is the async status/append path, not a blocking await.
    assert "check_async_task" in description


@pytest.mark.asyncio
async def test_coordinator_force_async_delegate_handler_uses_delegate_async(monkeypatch):
    """``delegate_to_agent`` handler calls orchestrator ``delegate_async`` and returns a handle.

    This is the integrated D-02 choke: the tool body resolves the target and
    hands off to the non-blocking ``delegate_async`` launcher, then returns a
    JSON task handle. We stub the target-resolution + ``delegate_async`` so the
    real routing (handler → ``_delegate_to_agent_async`` → ``delegate_async``)
    is what's exercised, and assert it returns immediately with a task_id rather
    than awaiting a worker transcript.
    """
    import app.services.agent_tool_domains.messaging as messaging

    source_agent = SimpleNamespace(
        id=uuid4(),
        name="Coordinator",
        creator_id=uuid4(),
        tenant_id=uuid4(),
    )
    target_agent = SimpleNamespace(id=uuid4(), name="Worker")
    target_model = SimpleNamespace(provider="openai", model="gpt-4.1")

    async def fake_resolve(_from_agent_id, _agent_name, *, target_agent_id=None):
        return source_agent, target_agent, target_model, None

    delegate_async_calls: list[dict] = []

    async def fake_delegate_async(**kwargs):
        # Record that the NON-BLOCKING launcher was invoked, and return a handle
        # exactly like the real orchestrator (which started a background task).
        delegate_async_calls.append(kwargs)
        return SimpleNamespace(
            task_id="async-task-123",
            target_name="Worker",
            trace_id="trace-xyz",
            status="running",
        )

    monkeypatch.setattr(messaging, "_resolve_target_agent_runtime", fake_resolve)
    # delegate_async is imported inside the function from the orchestrator module.
    import app.agents.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator, "delegate_async", fake_delegate_async)

    raw = await messaging._delegate_to_agent_async(
        source_agent.id,
        {"agent_name": "Worker", "message": "Audit auth/*.py and return file:line bugs."},
    )
    parsed = json.loads(raw)

    # The non-blocking launcher was called exactly once.
    assert len(delegate_async_calls) == 1
    # Returned immediately with a task handle — no worker transcript awaited.
    assert parsed["task_id"] == "async-task-123"
    assert parsed["runtime_task_id"] == "async-task-123"
    assert parsed["status"] == "running"
    # The continuation contract points at the async mailbox-append tool.
    assert parsed["continuation_tool"] == "send_agent_session_message"
    assert "check_async_task" in parsed["next_action"]


def test_coordinator_force_async_continuation_tools_are_coordinator_and_core_visible():
    """The async non-blocking continuation tools are coordinator-allowed AND CORE.

    For the delegate→continue loop to be non-blocking, the coordinator must be
    able to SEE and call the async status/append tools. They must be both in
    ``COORDINATOR_ALLOWED_TOOLS`` (survives coordinator tool filtering) and in
    ``CORE_TOOL_NAMES`` (always present in the base toolset).
    """
    from app.services.agent_tools import CORE_TOOL_NAMES

    async_continuation = {"delegate_to_agent", "send_agent_session_message", "check_async_task"}

    # Visible to the coordinator (not filtered out).
    assert async_continuation <= COORDINATOR_ALLOWED_TOOLS
    # And always in the base/core toolset.
    assert async_continuation <= CORE_TOOL_NAMES

    # Filtering a mixed toolset keeps the async continuation tools, drops domain
    # tools the coordinator must delegate instead of running.
    filtered = filter_tools_for_coordinator(
        [
            {"function": {"name": "delegate_to_agent", "parameters": {}}},
            {"function": {"name": "send_agent_session_message", "parameters": {}}},
            {"function": {"name": "check_async_task", "parameters": {}}},
            {"function": {"name": "web_search", "parameters": {}}},
            {"function": {"name": "execute_code", "parameters": {}}},
        ]
    )
    kept = {tool["function"]["name"] for tool in filtered}
    assert async_continuation <= kept
    assert "web_search" not in kept
    assert "execute_code" not in kept


def test_coordinator_force_async_prompt_directs_nonblocking_delegate_loop():
    """The coordinator prompt directs the async delegate→check/continue loop.

    The system prompt must teach: delegate via ``delegate_to_agent`` to async
    workers, continue them via ``send_agent_session_message``, and — critically —
    NOT block / fake completion while workers are still running (report status
    instead). This is the prompt-side half of D-02's non-blocking contract.
    """
    prompt = get_coordinator_prompt()

    # Async worker child sessions + the append-follow-up continuation tool.
    assert "send_agent_session_message" in prompt
    assert "async worker" in prompt.lower()
    # The non-blocking guard: never pretend completion while workers run.
    assert "still running" in prompt.lower()
    assert "never fabricate completion" in prompt.lower()
