"""CCPlus AgentTool compatibility contract for session-local workers."""

from __future__ import annotations


def test_spawn_subagent_schema_exposes_agenttool_compatible_fields() -> None:
    from app.tools.handlers.subagent import _SPAWN_PARAMETERS

    properties = _SPAWN_PARAMETERS["properties"]
    for field in (
        "description",
        "prompt",
        "subagent_type",
        "model",
        "run_in_background",
        "name",
        "team_name",
        # Compatibility aliases.
        "task",
        "type",
        "definition_name",
    ):
        assert field in properties, field

    assert "general-purpose" in properties["subagent_type"]["enum"]
    assert "explorer" in properties["subagent_type"]["enum"]
    assert "critic" in properties["subagent_type"]["enum"]
    assert {"required": ["prompt"]} in _SPAWN_PARAMETERS["anyOf"]
    assert {"required": ["task"]} in _SPAWN_PARAMETERS["anyOf"]


def test_spawn_subagent_description_draws_session_worker_employee_boundary() -> None:
    from app.tools.handlers.communication import delegate_to_agent
    from app.tools.handlers.subagent import spawn_subagent_tool

    spawn_desc = spawn_subagent_tool.meta.description
    delegate_desc = delegate_to_agent.meta.description

    assert "session-local worker" in spawn_desc
    assert "To Session Worker" in spawn_desc
    assert "standalone digital employee" in spawn_desc
    assert "To Employee" in delegate_desc
    assert "not a session-local worker" in delegate_desc
