from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.tools.runtime import ToolExecutionContext
from app.tools.service import _inject_runtime_context_arguments


def test_a2a_runtime_context_injects_stable_turn_and_task_idempotency_anchors() -> None:
    runtime_task_id = uuid.uuid4()
    context = ToolExecutionContext(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tenant_id=str(uuid.uuid4()),
        workspace=Path("/tmp/hive-runtime-context-test"),
        session_id="session-1",
        turn_id="turn-7",
        runtime_task_id=str(runtime_task_id),
    )

    enriched = _inject_runtime_context_arguments(
        "delegate_to_agent",
        {"agent_name": "Local Mac", "message": "Inspect the repository."},
        context,
    )

    assert enriched["parent_session_id"] == "session-1"
    assert enriched["_turn_id"] == "turn-7"
    assert enriched["_runtime_task_id"] == str(runtime_task_id)
    assert enriched["_requester_user_id"] == str(context.user_id)
    assert enriched["_execution_principal"] == {
        "schema": "hive.execution_principal.v1",
        "tenant_id": context.tenant_id,
        "source_agent_id": str(context.agent_id),
        "requester_user_id": str(context.user_id),
        "root_session_id": "session-1",
        "root_runtime_task_id": str(runtime_task_id),
        "origin": "agent_tool",
        "delegation_chain": [],
    }


@pytest.mark.asyncio
async def test_scoped_configuration_failure_preserves_identity_and_never_falls_back(monkeypatch, tmp_path):
    from app.core.execution_context import get_tool_agent_id, get_tool_tenant_id, tool_configuration_context
    from app.services.agent_tool_domains import web_mcp
    from app.tools.runtime import ToolExecutionRegistry, ToolExecutionRequest

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("synthetic configuration store unavailable")

    def forbidden_fallback():
        raise AssertionError("scoped configuration must not use a platform credential fallback")

    monkeypatch.setattr("app.services.tool_config_service.resolve_tool_config", unavailable)
    monkeypatch.setattr(web_mcp, "async_session", forbidden_fallback)
    registry = ToolExecutionRegistry()
    registry.register("probe", lambda _request: web_mcp._get_tool_config("exa_search"))
    context = ToolExecutionContext(agent_id=uuid.uuid4(), user_id=uuid.uuid4(), tenant_id=str(uuid.uuid4()), workspace=tmp_path)
    outer_tenant, outer_agent = uuid.uuid4(), uuid.uuid4()
    with tool_configuration_context(tenant_id=outer_tenant, agent_id=outer_agent):
        with pytest.raises(RuntimeError, match="synthetic configuration store unavailable"):
            await registry.try_execute(ToolExecutionRequest(tool_name="probe", arguments={}, context=context))
        assert get_tool_agent_id() == outer_agent and get_tool_tenant_id() == outer_tenant
