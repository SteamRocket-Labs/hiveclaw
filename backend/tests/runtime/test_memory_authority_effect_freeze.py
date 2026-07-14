from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_memory_authority_failure_freezes_mutation_but_keeps_reads_available() -> None:
    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest
    from app.runtime.session import SessionContext

    calls: list[str] = []

    async def executor(tool_name: str, _arguments: dict, **_kwargs):
        calls.append(tool_name)
        return f"ran:{tool_name}"

    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "inspect, then edit"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=uuid4(),
        tool_executor=executor,
        session_context=SessionContext(
            session_id=str(uuid4()),
            metadata={
                "memory_context_status": {
                    "status": "unavailable",
                    "authority_context_available": False,
                    "external_effects_available": False,
                }
            },
        ),
    )

    read_result = await invoker._execute_tool_with_request("read_file", {"path": "README.md"}, request, lambda _: None)
    write_result = await invoker._execute_tool_with_request(
        "write_file",
        {"path": "result.md", "content": "unsafe without authority"},
        request,
        lambda _: None,
    )

    assert read_result == "ran:read_file"
    assert calls == ["read_file"]
    assert "<tool_error>" in str(write_result)
    assert '"error_class": "authority_context_unavailable"' in str(write_result)
    assert '"outcome": "unavailable"' in str(write_result)
