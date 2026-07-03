"""CCPlus AgentTool compatibility contract for session-local workers."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest


def _request(arguments: dict, *, round_state: dict | None = None) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        tool_name="spawn_subagent",
        arguments=arguments,
        context=ToolExecutionContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            workspace=Path("/tmp"),
            session_id=str(uuid4()),
            round_state=round_state,
        ),
    )


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
        "mode",
        "isolation",
        # Compatibility aliases.
        "task",
        "type",
        "definition_name",
    ):
        assert field in properties, field

    assert "general-purpose" in properties["subagent_type"]["enum"]
    assert "explorer" in properties["subagent_type"]["enum"]
    assert "critic" in properties["subagent_type"]["enum"]
    assert "worker" not in properties["subagent_type"]["enum"]
    assert "worker" not in properties["type"]["enum"]
    assert properties["isolation"]["enum"] == ["none", "all"]
    assert properties["permission_profile"]["properties"]["allowed_tools"]["items"]["type"] == "string"
    assert {"required": ["prompt"]} in _SPAWN_PARAMETERS["anyOf"]
    assert {"required": ["task"]} in _SPAWN_PARAMETERS["anyOf"]


def test_spawn_subagent_normalizes_worker_alias_and_fork_semantics() -> None:
    from app.tools.handlers.subagent import _normalize_spawn_arguments

    normalized = _normalize_spawn_arguments({"task": "edit this file", "type": "worker"})

    assert normalized["type"] == "general-purpose"
    assert normalized["subagent_type"] == "general-purpose"

    forked = _normalize_spawn_arguments({"prompt": "survey current state"})
    assert forked["isolation"] == "all"
    assert forked["subagent_type"] == "general-purpose"

    background = _normalize_spawn_arguments({"prompt": "survey current state", "run_in_background": True})
    assert background["isolation"] == "none"
    assert background["subagent_type"] == "general-purpose"

    background_fork = _normalize_spawn_arguments(
        {"prompt": "survey current state", "run_in_background": True, "isolation": "all"}
    )
    assert background_fork["isolation"] == "all"

    fresh = _normalize_spawn_arguments({"prompt": "survey current state", "subagent_type": "explorer"})
    assert fresh["isolation"] == "none"
    assert fresh["subagent_type"] == "explorer"


def test_worker_alias_is_not_a_second_builtin_type() -> None:
    from app.agents.subagent import _TYPE_PRESETS, canonical_subagent_type, resolve_subagent_tools
    from app.agents.subagent import SubagentSpec

    assert "worker" not in _TYPE_PRESETS
    assert canonical_subagent_type("worker") == "general-purpose"
    allowed, _excluded = resolve_subagent_tools(SubagentSpec(name="legacy-worker", type="worker"))
    assert "write_file" in allowed
    assert "edit_file" in allowed


def test_spawn_subagent_permission_profile_allowed_tools_are_scoped() -> None:
    from app.tools.handlers.subagent import _permission_profile_allowed_tools

    assert _permission_profile_allowed_tools(
        {
            "permission_profile": {
                "allowed_tools": ["web_search", "read_file", "web_search", ""],
            }
        }
    ) == ("web_search", "read_file")
    assert _permission_profile_allowed_tools({"permission_profile": {"allowed_tools": "web_search"}}) == ()


@pytest.mark.asyncio
async def test_spawn_subagent_team_name_and_name_routes_to_teammate_branch(monkeypatch) -> None:
    import app.tools.handlers.subagent as handler_mod

    captured: dict = {}

    async def fake_spawn_teammate(request, **kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "status": "teammate_spawned",
            "team_name": kwargs["team_name"],
            "member_name": kwargs["member_name"],
            "subagent_type": kwargs["subagent_type"],
            "prompt": kwargs["prompt"],
        }

    monkeypatch.setattr(handler_mod, "spawn_agent_team_member_from_tool_request", fake_spawn_teammate)

    out = await handler_mod.spawn_subagent_tool(
        _request(
            {
                "team_name": "parity-review",
                "name": "critic",
                "subagent_type": "critic",
                "description": "Review the implementation",
                "prompt": "Check the AgentTool alignment.",
            }
        )
    )
    payload = json.loads(out)

    assert payload["ok"] is True
    assert payload["status"] == "teammate_spawned"
    assert captured == {
        "team_name": "parity-review",
        "member_name": "critic",
        "prompt": "Check the AgentTool alignment.",
        "description": "Review the implementation",
        "subagent_type": "critic",
        "model": "",
        "mode": "",
    }


@pytest.mark.asyncio
async def test_spawn_subagent_rejects_plain_worker_when_plan_requires_agent_team() -> None:
    import app.tools.handlers.subagent as handler_mod

    out = await handler_mod.spawn_subagent_tool(
        _request(
            {
                "prompt": "Split this into the approved team.",
                "subagent_type": "critic",
            },
            round_state={
                "execution_contract": {
                    "type": "agent_team",
                    "name": "ABS research team",
                    "members": [{"name": "structure-analyst"}],
                }
            },
        )
    )
    payload = json.loads(out)

    assert payload == {
        "ok": False,
        "error": "agent_team_contract_required",
        "message": (
            "The confirmed plan requires Agent Team execution. Call team_create first, then spawn teammates "
            "with spawn_subagent using both team_name and name."
        ),
        "required_tool": "team_create",
        "teammate_creation_tool": "spawn_subagent",
        "expected_arguments": {"team_name": "ABS research team", "name": "<member_name>"},
        "contract_type": "agent_team",
    }


@pytest.mark.asyncio
async def test_spawn_subagent_rejects_plain_worker_when_session_has_active_agent_team(monkeypatch) -> None:
    import app.tools.handlers.subagent as handler_mod

    async def fake_active_team_contract(_request):
        return {
            "type": "agent_team",
            "source": "active_session_team",
            "team_id": "team-123",
            "name": "金融创新产品族研究 Team",
        }

    async def fail_resolve_parent_runtime(_agent_id):  # pragma: no cover - must not reach plain worker path
        raise AssertionError("plain subagent runtime must not start while an active Agent Team contract exists")

    monkeypatch.setattr(
        handler_mod,
        "active_agent_team_contract_from_tool_request",
        fake_active_team_contract,
        raising=False,
    )
    monkeypatch.setattr(handler_mod, "_resolve_parent_runtime", fail_resolve_parent_runtime)

    out = await handler_mod.spawn_subagent_tool(
        _request(
            {
                "name": "scenario-expert",
                "subagent_type": "explorer",
                "prompt": "Research scenarios for the Agent Team.",
            }
        )
    )
    payload = json.loads(out)

    assert payload["ok"] is False
    assert payload["error"] == "agent_team_contract_required"
    assert payload["required_tool"] == "team_create"
    assert payload["teammate_creation_tool"] == "spawn_subagent"
    assert payload["expected_arguments"] == {
        "team_name": "金融创新产品族研究 Team",
        "name": "<member_name>",
    }
    assert payload["team_id"] == "team-123"


def test_spawn_subagent_description_draws_session_worker_employee_boundary() -> None:
    from app.tools.handlers.communication import delegate_to_agent
    from app.tools.handlers.subagent import spawn_subagent_tool

    spawn_desc = spawn_subagent_tool.meta.description
    delegate_desc = delegate_to_agent.meta.description

    assert "AgentTool" in spawn_desc
    assert "team_name" in spawn_desc
    assert "session-local worker" in spawn_desc
    assert "To Session Worker" in spawn_desc
    assert "standalone digital employee" in spawn_desc
    assert "To Employee" in delegate_desc
    assert "not a session-local worker" in delegate_desc
