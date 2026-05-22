from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.chat_sessions as chat_sessions_api
from app.core.security import get_current_user
from app.database import get_db


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, session):
        self.session = session

    async def execute(self, _stmt):
        return _ScalarResult(self.session)


def _client(monkeypatch, *, db, user, agent, access_level="use"):
    app = FastAPI()
    app.include_router(chat_sessions_api.router)

    async def override_user():
        return user

    async def override_db():
        yield db

    async def allow_access(_db, _user, agent_id):
        assert agent_id == agent.id
        return agent, access_level

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(chat_sessions_api, "check_agent_access", allow_access)
    return TestClient(app)


def test_start_session_run_routes_to_runtime_service(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    db = _FakeDB(session)
    captured = {}

    async def fake_start(**kwargs):
        captured.update(kwargs)
        return {"run_id": "run-1", "status": "running"}

    monkeypatch.setattr(chat_sessions_api, "start_web_chat_run", fake_start)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/runs",
        json={"content": "hello", "display_content": "hello", "file_name": ""},
    )

    assert response.status_code == 201
    assert response.json() == {"run_id": "run-1", "status": "running"}
    assert captured["db"] is db
    assert captured["agent"] is agent
    assert captured["user"] is user
    assert captured["session"] is session
    assert captured["content"] == "hello"


def test_start_session_run_rejects_non_owner_without_manage_access(monkeypatch):
    agent_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=uuid4())
    db = _FakeDB(session)
    client = _client(monkeypatch, db=db, user=user, agent=agent, access_level="use")

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/runs",
        json={"content": "hello"},
    )

    assert response.status_code == 403


def test_active_session_run_endpoint_returns_runtime_payload(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    db = _FakeDB(session)

    async def fake_active(**kwargs):
        return {"run_id": "run-1", "status": "running"}

    monkeypatch.setattr(chat_sessions_api, "get_active_web_chat_run", fake_active)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.get(f"/agents/{agent_id}/sessions/{session_id}/runs/active")

    assert response.status_code == 200
    assert response.json() == {"run_id": "run-1", "status": "running"}


def test_cancel_session_run_routes_to_runtime_service(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    db = _FakeDB(session)
    captured = {}

    async def fake_cancel(**kwargs):
        captured.update(kwargs)
        return {"run_id": run_id.hex, "status": "killed"}

    monkeypatch.setattr(chat_sessions_api, "cancel_web_chat_run", fake_cancel)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(f"/agents/{agent_id}/sessions/{session_id}/runs/{run_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "killed"
    assert captured["run_id"] == run_id
    assert captured["user_id"] == user_id
