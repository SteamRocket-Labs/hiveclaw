"""Tests for coordinator mode."""

from __future__ import annotations

from app.runtime.coordinator import (
    COORDINATOR_ALLOWED_TOOLS,
    filter_tools_for_coordinator,
    get_coordinator_prompt,
    is_coordinator_mode,
)


class TestCoordinatorMode:
    """Coordinator mode detection and tool filtering."""

    def test_not_active_by_default(self) -> None:
        assert is_coordinator_mode() is False

    def test_active_via_request(self) -> None:
        class FakeRequest:
            execution_mode = "coordinator"

        assert is_coordinator_mode(request=FakeRequest()) is True

    def test_active_via_agent(self) -> None:
        class FakeAgent:
            execution_mode = "coordinator"

        assert is_coordinator_mode(agent=FakeAgent()) is True

    def test_not_active_for_normal_mode(self) -> None:
        class FakeRequest:
            execution_mode = "normal"

        assert is_coordinator_mode(request=FakeRequest()) is False

    def test_prompt_contains_coordinator_rules(self) -> None:
        prompt = get_coordinator_prompt()
        assert "Coordinator Mode" in prompt
        assert "delegate_to_agent" in prompt
        assert "never delegate understanding" in prompt.lower()
        assert "Use `read_file`, `write_file`, and `list_files` only for coordination artifacts" in prompt
        assert "When workers are still running, give the user a status update" in prompt
        assert "Status:" in prompt
        assert "Synthesis:" in prompt

    def test_filter_keeps_only_allowed_tools(self) -> None:
        tools = [
            {"function": {"name": "delegate_to_agent", "parameters": {}}},
            {"function": {"name": "web_search", "parameters": {}}},
            {"function": {"name": "read_file", "parameters": {}}},
            {"function": {"name": "execute_code", "parameters": {}}},
        ]
        filtered = filter_tools_for_coordinator(tools)
        names = {t["function"]["name"] for t in filtered}
        assert "delegate_to_agent" in names
        assert "read_file" in names
        assert "web_search" not in names
        assert "execute_code" not in names

    def test_filter_empty_tools(self) -> None:
        assert filter_tools_for_coordinator([]) == []

    def test_all_allowed_tools_are_reasonable(self) -> None:
        # Coordinator should have delegation + file access + time + triggers
        assert "delegate_to_agent" in COORDINATOR_ALLOWED_TOOLS
        assert "cancel_async_task" in COORDINATOR_ALLOWED_TOOLS
        assert "send_message_to_agent" in COORDINATOR_ALLOWED_TOOLS
        assert "check_async_task" in COORDINATOR_ALLOWED_TOOLS
        assert "read_file" in COORDINATOR_ALLOWED_TOOLS
        # But NOT domain tools
        assert "web_search" not in COORDINATOR_ALLOWED_TOOLS
        assert "execute_code" not in COORDINATOR_ALLOWED_TOOLS

    def test_all_allowed_tools_exist_in_real_registry(self) -> None:
        from app.services.agent_tools import get_combined_openai_tools

        names = {tool["function"]["name"] for tool in get_combined_openai_tools()}
        missing = COORDINATOR_ALLOWED_TOOLS - names
        assert missing == set()
