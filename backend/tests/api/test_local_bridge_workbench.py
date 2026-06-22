from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.local_bridge as local_bridge_api
from app.core.security import get_current_user
from app.database import get_db


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def all(self):
        if isinstance(self._value, list):
            return list(self._value)
        if self._value is None:
            return []
        return [self._value]

    def scalar_one_or_none(self):
        if isinstance(self._value, list):
            if len(self._value) > 1:
                raise AssertionError("scalar_one_or_none received multiple values")
            return self._value[0] if self._value else None
        return self._value


class _FakeDB:
    def __init__(self, execute_values):
        self.execute_values = list(execute_values)

    async def execute(self, _stmt):
        if not self.execute_values:
            raise AssertionError("Unexpected execute call")
        return _FakeResult(self.execute_values.pop(0))


def _client(monkeypatch, *, current_user, db, agent):
    app = FastAPI()
    app.include_router(local_bridge_api.router)

    async def override_user():
        return current_user

    async def override_db():
        yield db

    async def fake_check_agent_access(db_arg, user_arg, requested_agent_id):
        assert db_arg is db
        assert user_arg is current_user
        assert requested_agent_id == agent.id
        return agent, "use"

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(local_bridge_api, "check_agent_access", fake_check_agent_access)
    return TestClient(app)


def _work_request(*, agent_id, tenant_id, user_id, status, result=None):
    now = datetime(2026, 6, 22, 6, 20, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        tenant_id=tenant_id,
        sender_user_id=user_id,
        conversation_id=str(uuid4()),
        content="Upload a markdown report to workspace",
        status=status,
        result=result,
        attachments_json=[{"path": "workspace/local-bridge/report.md", "direction": "result"}] if result else [],
        metadata_json={"kind": "work_request", "priority": "normal"},
        created_at=now,
        delivered_at=now if status in {"delivered", "completed"} else None,
        completed_at=now if status == "completed" else None,
    )


def test_local_bridge_workbench_lists_current_users_work_requests(monkeypatch) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    agent = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    current_user = SimpleNamespace(id=user_id, tenant_id=tenant_id, role="member")
    completed = _work_request(
        agent_id=agent.id,
        tenant_id=tenant_id,
        user_id=user_id,
        status="completed",
        result="done by local command runtime",
    )
    pending = _work_request(agent_id=agent.id, tenant_id=tenant_id, user_id=user_id, status="pending")
    db = _FakeDB([[completed, pending]])
    client = _client(monkeypatch, current_user=current_user, db=db, agent=agent)

    response = client.get(f"/agents/{agent.id}/local-bridge/work-requests")

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload["work_requests"]] == [str(completed.id), str(pending.id)]
    assert payload["work_requests"][0]["status"] == "completed"
    assert payload["work_requests"][0]["result"] == "done by local command runtime"
    assert payload["work_requests"][0]["attachments"] == [
        {"path": "workspace/local-bridge/report.md", "direction": "result"}
    ]
    assert payload["work_requests"][0]["metadata"]["priority"] == "normal"


def test_local_bridge_workbench_gets_one_work_request(monkeypatch) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    agent = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    current_user = SimpleNamespace(id=user_id, tenant_id=tenant_id, role="member")
    message = _work_request(
        agent_id=agent.id,
        tenant_id=tenant_id,
        user_id=user_id,
        status="completed",
        result="done",
    )
    db = _FakeDB([message])
    client = _client(monkeypatch, current_user=current_user, db=db, agent=agent)

    response = client.get(f"/agents/{agent.id}/local-bridge/work-requests/{message.id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(message.id)
    assert response.json()["conversation_id"] == message.conversation_id
