from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.flushed = False

    async def execute(self, _stmt):
        return self._results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed = True


@pytest.mark.asyncio
async def test_delegate_task_routes_through_runtime_delegation(monkeypatch):
    from app.services.collaboration import collaboration_service

    from_agent_id = uuid4()
    to_agent_id = uuid4()
    source_agent = SimpleNamespace(id=from_agent_id, name="源代理", creator_id=uuid4(), tenant_id=uuid4())
    target_agent = SimpleNamespace(id=to_agent_id, name="目标代理", status="running", tenant_id=source_agent.tenant_id)
    db = _FakeDB([_ScalarResult(source_agent), _ScalarResult(target_agent)])
    captured = {}

    async def fake_delegate(from_agent_id_arg, args):
        captured["from_agent_id"] = from_agent_id_arg
        captured["args"] = args
        return json.dumps({
            "task_id": "runtime-task-1",
            "status": "running",
            "target_agent": "目标代理",
            "trace_id": "trace-1",
        }, ensure_ascii=False)

    monkeypatch.setattr("app.services.agent_tool_domains.messaging._delegate_to_agent_async", fake_delegate)

    result = await collaboration_service.delegate_task(
        db,
        from_agent_id=from_agent_id,
        to_agent_id=to_agent_id,
        task_title="整理需求",
        task_description="输出要点和风险",
    )

    assert captured["from_agent_id"] == from_agent_id
    assert captured["args"]["agent_name"] == "目标代理"
    assert captured["args"]["message"] == "整理需求\n\n输出要点和风险"
    assert result["task_id"] == "runtime-task-1"
    assert result["status"] == "running"
    assert result["from_agent"] == "源代理"
    assert result["to_agent"] == "目标代理"
    assert db.flushed is True
    assert len(db.added) == 1
    assert db.added[0].action == "collaboration:delegation"
    assert db.added[0].details == {
        "from_agent": str(from_agent_id),
        "to_agent": str(to_agent_id),
        "task_title": "整理需求",
        "runtime_task_id": "runtime-task-1",
        "trace_id": "trace-1",
        "interaction_type": "delegation",
        "route": "runtime_delegation",
    }


@pytest.mark.asyncio
async def test_send_message_between_agents_routes_through_runtime_agent_message(monkeypatch):
    from app.services.collaboration import collaboration_service

    from_agent_id = uuid4()
    to_agent_id = uuid4()
    source_agent = SimpleNamespace(id=from_agent_id, name="源代理", creator_id=uuid4(), tenant_id=uuid4())
    target_agent = SimpleNamespace(id=to_agent_id, name="目标代理", status="running", tenant_id=source_agent.tenant_id)
    db = _FakeDB([_ScalarResult(source_agent), _ScalarResult(target_agent)])
    captured = {}

    async def fake_send_message(from_agent_id_arg, args):
        captured["from_agent_id"] = from_agent_id_arg
        captured["args"] = args
        return "💬 目标代理 replied:\n已收到"

    monkeypatch.setattr("app.services.agent_tool_domains.messaging._send_message_to_agent", fake_send_message)

    result = await collaboration_service.send_message_between_agents(
        db,
        from_agent_id=from_agent_id,
        to_agent_id=to_agent_id,
        message="请确认方案边界",
        msg_type="consult",
    )

    assert captured["from_agent_id"] == from_agent_id
    assert captured["args"] == {
        "agent_name": "目标代理",
        "target_agent_id": str(to_agent_id),
        "message": "请确认方案边界",
        "msg_type": "consult",
    }
    assert result["status"] == "completed"
    assert result["type"] == "consult"
    assert result["from_agent"] == "源代理"
    assert result["to_agent"] == "目标代理"
    assert result["result"] == "💬 目标代理 replied:\n已收到"
    assert db.flushed is True
    assert len(db.added) == 1
    assert db.added[0].action == "collaboration:agent_message"
    assert db.added[0].details == {
        "from_agent": str(from_agent_id),
        "to_agent": str(to_agent_id),
        "msg_type": "consult",
        "message_preview": "请确认方案边界",
        "interaction_type": "agent_message",
        "route": "runtime_agent_message",
        "result_preview": "💬 目标代理 replied:\n已收到",
    }
