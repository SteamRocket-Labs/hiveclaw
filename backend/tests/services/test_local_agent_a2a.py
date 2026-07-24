from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.agent_tool_domains import messaging


@pytest.mark.asyncio
async def test_delegate_to_agent_routes_execution_target_local_agent(monkeypatch) -> None:
    from_agent_id = uuid4()
    source = SimpleNamespace(id=from_agent_id, name="Coordinator", tenant_id=uuid4(), creator_id=uuid4())
    target = SimpleNamespace(id=uuid4(), name="Local Codex", tenant_id=source.tenant_id, status="running")
    captured = {}

    async def fake_resolve_target_agent_runtime(from_agent_id_arg, agent_name, *, target_agent_id=None):
        captured["resolve"] = {
            "from_agent_id": from_agent_id_arg,
            "agent_name": agent_name,
            "target_agent_id": target_agent_id,
        }
        return source, target, None, None

    async def fake_delegate_to_local_agent_channel(*, source_agent, target_agent, message_text, args):
        captured["local"] = {
            "source_agent": source_agent,
            "target_agent": target_agent,
            "message_text": message_text,
            "args": args,
        }
        return {
            "status": "queued",
            "execution_target": "local_agent",
            "target_agent": target_agent.name,
            "channel_session_id": "session-1",
            "message_id": "message-1",
        }

    async def should_not_delegate_async(*_args, **_kwargs):
        raise AssertionError("cloud async delegation must not run for execution_target=local_agent")

    monkeypatch.setattr(messaging, "_resolve_target_agent_runtime", fake_resolve_target_agent_runtime)
    monkeypatch.setattr(messaging, "_delegate_to_local_agent_channel", fake_delegate_to_local_agent_channel)
    monkeypatch.setattr("app.agents.orchestrator.delegate_async", should_not_delegate_async)

    result = await messaging._delegate_to_agent_async(
        from_agent_id,
        {
            "agent_name": "Local Codex",
            "message": "run this on the local machine",
            "execution_target": "local_agent",
            "expected_output": "short confirmation",
        },
    )

    payload = json.loads(result)
    assert payload["status"] == "queued"
    assert payload["execution_target"] == "local_agent"
    assert payload["message_id"] == "message-1"
    assert captured["resolve"]["agent_name"] == "Local Codex"
    assert captured["local"]["source_agent"] is source
    assert captured["local"]["target_agent"] is target
    assert captured["local"]["message_text"] == "run this on the local machine"


def test_check_async_task_schema_accepts_runtime_task_or_local_message_id() -> None:
    from app.tools.handlers.communication import check_async_task

    parameters = check_async_task.meta.parameters

    assert parameters.get("required", []) == []
    assert parameters["anyOf"] == [
        {"required": ["task_id"]},
        {"required": ["message_id"]},
    ]
    assert parameters["properties"]["task_id"]["type"] == "string"
    assert parameters["properties"]["message_id"]["type"] == "string"


@pytest.mark.asyncio
async def test_check_async_task_requires_exactly_one_async_handle() -> None:
    from app.services.agent_tool_domains.messaging import _check_async_task

    missing = await _check_async_task(uuid4(), {})
    ambiguous = await _check_async_task(
        uuid4(),
        {"task_id": "runtime-task", "message_id": str(uuid4())},
    )

    assert "exactly one of task_id or message_id" in missing
    assert "exactly one of task_id or message_id" in ambiguous
