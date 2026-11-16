"""A failed memory database read stays inside the tool's typed error boundary."""

from contextlib import asynccontextmanager
import json
import uuid

import pytest

from app.tools.runtime import ToolExecutionContext


@pytest.mark.parametrize("tool", ["search_memory", "load_memory"])
async def test_memory_connection_failure_returns_the_base_error(monkeypatch, tmp_path, tool):
    from app import database
    from app.services import explicit_memory_authority, explicit_memory_tool_sources, team_memory_runtime
    from app.tools.handlers import memory

    @asynccontextmanager
    async def unavailable(*_args, **_kwargs):
        raise ConnectionRefusedError("local database unavailable")
        yield  # pragma: no cover

    for module in (database, explicit_memory_authority, explicit_memory_tool_sources, team_memory_runtime):
        monkeypatch.setattr(module, "tenant_scoped_session", unavailable)
    context = ToolExecutionContext(agent_id=uuid.uuid4(), user_id=uuid.uuid4(), tenant_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()), runtime_task_id=str(uuid.uuid4()), workspace=tmp_path)
    args = {"query": "release", "scope": "facts"} if tool == "search_memory" else {"ids": ["entry"]}
    result = await getattr(memory, tool)(context.agent_id, args, execution_context=context)
    payload = json.loads(str(result).split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])
    assert payload["reason_code"] == "explicit_memory_operation_unknown"
    assert payload["outcome"] == "unknown"
