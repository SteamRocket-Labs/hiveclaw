"""Red-first contracts for the EnvironmentService production wiring."""

from __future__ import annotations

import inspect
import uuid
from pathlib import Path

import pytest


def test_code_execution_facade_has_no_direct_provider_selection():
    from app.services.code_execution import service

    source = inspect.getsource(service)

    assert "get_environment_service" in source
    assert "code_execution.local_provider" not in source
    assert "code_execution.vercel_provider" not in source


@pytest.mark.asyncio
async def test_workspace_adapter_forwards_the_exact_runtime_context():
    from app.tools.adapters import adapt_and_call
    from app.tools.decorator import ToolMeta
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    context = ToolExecutionContext(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tenant_id=str(uuid.uuid4()),
        workspace=Path("/tmp/environment-contract-workspace"),
        runtime_task_id=str(uuid.uuid4()),
    )
    request = ToolExecutionRequest(tool_name="execute_code", arguments={"code": "print(1)"}, context=context)
    captured = {}

    async def handler(
        workspace: Path,
        arguments: dict,
        tenant_id: str | None,
        *,
        execution_context: ToolExecutionContext,
    ) -> str:
        captured.update(
            workspace=workspace,
            arguments=arguments,
            tenant_id=tenant_id,
            execution_context=execution_context,
        )
        return "ok"

    result = await adapt_and_call(
        ToolMeta(
            name="execute_code",
            description="contract",
            parameters={},
            category="filesystem",
            display_name="Execute Code",
            adapter="workspace_args",
        ),
        handler,
        request,
    )

    assert result == "ok"
    assert captured["execution_context"] is context


def test_environment_service_exposes_one_execute_entrypoint():
    from app.services.environments.service import EnvironmentService

    signature = inspect.signature(EnvironmentService.execute)

    assert "request" in signature.parameters
    assert "provider" not in signature.parameters
