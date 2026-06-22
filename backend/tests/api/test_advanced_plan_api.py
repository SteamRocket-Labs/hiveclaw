from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, *values) -> None:
        self._values = list(values)

    async def execute(self, _stmt):
        if not self._values:
            return _ScalarResult(None)
        return _ScalarResult(self._values.pop(0))


@pytest.mark.asyncio
async def test_advanced_plan_api_starts_advanced_plan_runtime(monkeypatch):
    import app.api.advanced_plan as advanced_plan_api

    agent_id = uuid4()
    session_id = uuid4()
    tenant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, name="Agent")
    session = SimpleNamespace(id=session_id, agent_id=agent_id)
    db = _FakeDB(session)
    captured = {}

    async def fake_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent_id
        return agent, "use"

    async def fake_start_web_chat_run(**kwargs):
        captured.update(kwargs)
        return {"run_id": "plan-run-1", "status": "running"}

    monkeypatch.setattr(advanced_plan_api, "check_agent_access", fake_access)
    monkeypatch.setattr(advanced_plan_api, "start_web_chat_run", fake_start_web_chat_run)

    result = await advanced_plan_api.start_advanced_plan(
        agent_id=agent_id,
        session_id=session_id,
        body=advanced_plan_api.StartAdvancedPlanIn(
            objective="Design the parity rollout.",
            context={"source": "freecode-parity"},
        ),
        current_user=current_user,
        db=db,
    )

    assert result["run_id"] == "plan-run-1"
    assert captured["agent"] is agent
    assert captured["user"] is current_user
    assert captured["session"] is session
    assert captured["runtime_task_type"] == "advanced_plan"
    assert captured["append_user_message"] is False
    assert captured["extra_metadata"]["advanced_plan"] is True
    assert captured["extra_metadata"]["context"] == {"source": "freecode-parity"}
