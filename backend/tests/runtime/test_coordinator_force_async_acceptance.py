"""CCPlus Workstream B acceptance: coordinator uses session-local AgentTool workers.

This replaces the old coordinator contract that treated ``delegate_to_agent`` as
the worker-spawn primitive. ``delegate_to_agent`` is A2A / To Employee. A
coordinator doing session-local decomposition must use ``spawn_subagent`` and
the subagent completion mailbox/wake path.
"""

from __future__ import annotations

from app.runtime.coordinator import (
    COORDINATOR_ALLOWED_TOOLS,
    filter_tools_for_coordinator,
    get_coordinator_prompt,
)


def test_coordinator_agenttool_surface_is_session_worker_variant() -> None:
    from app.services.agent_tools import get_combined_openai_tools

    spawn_specs = [tool for tool in get_combined_openai_tools() if tool["function"]["name"] == "spawn_subagent"]
    assert len(spawn_specs) == 1
    description = spawn_specs[0]["function"]["description"]
    parameters = spawn_specs[0]["function"]["parameters"]

    assert "To Session Worker" in description
    assert "session-local worker" in description
    assert "standalone digital employee" in description
    assert "delegate_to_agent" in description
    assert "prompt" in parameters["properties"]
    assert "subagent_type" in parameters["properties"]
    assert "enum" not in parameters["properties"]["subagent_type"]
    assert "general-purpose" in parameters["properties"]["subagent_type"]["description"]


def test_coordinator_session_worker_tools_are_coordinator_and_core_visible() -> None:
    from app.services.agent_tools import CORE_TOOL_NAMES

    worker_tools = {"spawn_subagent", "check_subagent", "send_agent_session_message"}

    assert worker_tools <= COORDINATOR_ALLOWED_TOOLS
    assert worker_tools <= CORE_TOOL_NAMES
    assert "delegate_to_agent" not in COORDINATOR_ALLOWED_TOOLS
    assert "check_async_task" not in COORDINATOR_ALLOWED_TOOLS

    tools = [
        {"function": {"name": "spawn_subagent", "parameters": {}}},
        {"function": {"name": "check_subagent", "parameters": {}}},
        {"function": {"name": "send_agent_session_message", "parameters": {}}},
        {"function": {"name": "delegate_to_agent", "parameters": {}}},
        {"function": {"name": "web_search", "parameters": {}}},
        {"function": {"name": "execute_code", "parameters": {}}},
    ]
    filtered = filter_tools_for_coordinator(tools)
    kept = {tool["function"]["name"] for tool in filtered}
    assert worker_tools <= kept
    assert {"delegate_to_agent", "web_search", "execute_code"} <= kept

    strict = filter_tools_for_coordinator(tools, dispatcher_only=True)
    strict_kept = {tool["function"]["name"] for tool in strict}
    assert worker_tools <= strict_kept
    assert {"delegate_to_agent", "web_search", "execute_code"}.isdisjoint(strict_kept)


def test_coordinator_prompt_directs_agenttool_nonblocking_worker_loop() -> None:
    prompt = get_coordinator_prompt()

    assert "To Session Worker" in prompt
    assert "spawn_subagent" in prompt
    assert "check_subagent" in prompt
    assert "send_agent_session_message" in prompt
    assert "delegate_to_agent" not in prompt
    assert "running work" in prompt.lower()
    assert "never fabricate completion" in prompt.lower()
