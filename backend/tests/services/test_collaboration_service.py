from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.execution_context import ExecutionPrincipal
from app.services.a2a_outcome import A2AOutcome


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

    requester_user_id = uuid4()
    principal = ExecutionPrincipal(
        tenant_id=source_agent.tenant_id,
        source_agent_id=from_agent_id,
        requester_user_id=requester_user_id,
        root_session_id="root-session-1",
        root_runtime_task_id="root-runtime-1",
        origin="rest",
    )

    async def fake_delegate(from_agent_id_arg, args, *, principal):
        captured["from_agent_id"] = from_agent_id_arg
        captured["args"] = args
        captured["principal"] = principal
        return A2AOutcome.success(
            operation="delegate",
            payload={
                "task_id": "runtime-task-1",
                "session_id": "child-session-1",
                "child_session_id": "child-session-1",
                "status": "running",
                "target_agent": "目标代理",
                "trace_id": "trace-1",
            },
        )

    monkeypatch.setattr("app.services.agent_tool_domains.messaging._delegate_to_agent_async_outcome", fake_delegate)

    result = await collaboration_service.delegate_task(
        db,
        from_agent_id=from_agent_id,
        to_agent_id=to_agent_id,
        task_title="整理需求",
        task_description="输出要点和风险",
        principal=principal,
    )

    assert captured["from_agent_id"] == from_agent_id
    assert captured["args"]["agent_name"] == "目标代理"
    assert captured["args"]["message"] == "整理需求\n\n输出要点和风险"
    assert captured["principal"] is principal
    assert result["task_id"] == "runtime-task-1"
    assert result["session_id"] == "child-session-1"
    assert result["child_session_id"] == "child-session-1"
    assert result["status"] == "running"
    assert result["from_agent"] == "源代理"
    assert result["to_agent"] == "目标代理"
    audit = db.added[0]
    assert audit.user_id == requester_user_id
    assert audit.details["execution_principal"]["root_session_id"] == "root-session-1"
    assert audit.details["execution_principal"]["root_runtime_task_id"] == "root-runtime-1"


@pytest.mark.asyncio
async def test_consult_failure_is_never_reported_as_sent(monkeypatch):
    from app.services.collaboration import collaboration_service

    from_agent_id = uuid4()
    to_agent_id = uuid4()
    tenant_id = uuid4()
    principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=from_agent_id,
        requester_user_id=uuid4(),
        root_session_id="root-session-2",
        origin="rest",
    )
    source_agent = SimpleNamespace(id=from_agent_id, name="源代理", creator_id=uuid4(), tenant_id=tenant_id)
    target_agent = SimpleNamespace(id=to_agent_id, name="目标代理", status="running", tenant_id=tenant_id)
    db = _FakeDB([_ScalarResult(source_agent), _ScalarResult(target_agent)])

    async def fake_consult(*_args, **_kwargs):
        return A2AOutcome.failure(
            operation="consult",
            error_code="provider_timeout",
            message="target provider timed out",
            retryable=True,
        )

    monkeypatch.setattr("app.services.agent_tool_domains.messaging._send_message_to_agent_outcome", fake_consult)

    with pytest.raises(ValueError, match="target provider timed out"):
        await collaboration_service.send_message_between_agents(
            db,
            from_agent_id,
            to_agent_id,
            "请给建议",
            "consult",
            principal=principal,
        )

    assert db.added == []
